import asyncio
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from asoul_calendar import CalendarRepository, _CalendarDownloadResult
from asoul_core import (
    CALENDAR_USER_AGENT,
    DISPLAY_TZ,
    CalendarEvent,
    ScheduleItem,
    parse_live_request,
)
from asoul_schedule import ScheduleService


class CalendarRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = CalendarRepository(cache_path=None)

    def _sample_ics(self, summary: str = "嘉然直播：晚间歌会") -> str:
        return "\n".join(
            [
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "DTSTART;TZID=Asia/Shanghai:20260330T200000",
                "DTEND;TZID=Asia/Shanghai:20260330T213000",
                f"SUMMARY:{summary}",
                "DESCRIPTION:嘉然 | 嘉然\\n唱歌专场",
                "LOCATION:B站直播间",
                "CATEGORIES:歌会",
                "URL:https://live.bilibili.com/12345",
                "STATUS:CONFIRMED",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        )

    def test_parse_calendar_decodes_multiline_event(self) -> None:
        ics_text = self._sample_ics()

        events = self.repository._parse_calendar(ics_text)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.summary, "嘉然直播：晚间歌会")
        self.assertEqual(event.description, "嘉然 | 嘉然\n唱歌专场")
        self.assertEqual(event.start, datetime(2026, 3, 30, 20, 0, tzinfo=DISPLAY_TZ))
        self.assertTrue(self.repository._is_livestream_event(event))

    def test_load_calendar_uses_memory_cache_with_asasfans_ua(self) -> None:
        repository = CalendarRepository(cache_path=None)
        calls = []

        def fake_download():
            calls.append(True)
            return _CalendarDownloadResult(
                text=self._sample_ics(),
                etag='"v1"',
                last_modified="Mon, 30 Mar 2026 12:00:00 GMT",
            )

        repository._download_calendar = fake_download

        first_events = asyncio.run(repository._load_calendar_events())
        second_events = asyncio.run(repository._load_calendar_events())

        self.assertEqual(len(calls), 1)
        self.assertEqual([event.summary for event in first_events], ["嘉然直播：晚间歌会"])
        self.assertEqual([event.summary for event in second_events], ["嘉然直播：晚间歌会"])
        self.assertEqual(repository._user_agent, CALENDAR_USER_AGENT)

    def test_expired_calendar_cache_reuses_body_on_not_modified_response(self) -> None:
        repository = CalendarRepository(cache_path=None)
        calls = []
        responses = [
            _CalendarDownloadResult(
                text=self._sample_ics(),
                etag='"v1"',
                last_modified="Mon, 30 Mar 2026 12:00:00 GMT",
            ),
            _CalendarDownloadResult(text=None, not_modified=True),
        ]

        def fake_download():
            calls.append((repository._calendar_etag, repository._calendar_last_modified))
            return responses.pop(0)

        repository._download_calendar = fake_download

        asyncio.run(repository._load_calendar_events())
        repository._calendar_cache_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        events = asyncio.run(repository._load_calendar_events())

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], ('"v1"', "Mon, 30 Mar 2026 12:00:00 GMT"))
        self.assertEqual([event.summary for event in events], ["嘉然直播：晚间歌会"])
        self.assertGreater(repository._calendar_cache_expires_at, datetime.now(timezone.utc))

    def test_refresh_failure_uses_stale_cache_and_backs_off(self) -> None:
        repository = CalendarRepository(
            cache_ttl=timedelta(seconds=0),
            cache_path=None,
            failure_retry_delay=timedelta(minutes=5),
        )
        call_count = 0

        def fake_download():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _CalendarDownloadResult(text=self._sample_ics())
            raise RuntimeError("calendar source unavailable")

        repository._download_calendar = fake_download

        asyncio.run(repository._load_calendar_events())
        stale_events = asyncio.run(repository._load_calendar_events())
        backed_off_events = asyncio.run(repository._load_calendar_events())

        self.assertEqual(call_count, 2)
        self.assertEqual([event.summary for event in stale_events], ["嘉然直播：晚间歌会"])
        self.assertEqual([event.summary for event in backed_off_events], ["嘉然直播：晚间歌会"])

    def test_fresh_disk_cache_is_used_after_repository_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "calendar_cache.json"
            first_repository = CalendarRepository(cache_path=cache_path)

            def fake_download():
                return _CalendarDownloadResult(
                    text=self._sample_ics("磁盘缓存直播"),
                    etag='"v1"',
                    last_modified="Mon, 30 Mar 2026 12:00:00 GMT",
                )

            first_repository._download_calendar = fake_download
            asyncio.run(first_repository._load_calendar_events())

            second_repository = CalendarRepository(cache_path=cache_path)
            download_called = False

            def unexpected_download():
                nonlocal download_called
                download_called = True
                raise AssertionError("fresh disk cache should avoid source request")

            second_repository._download_calendar = unexpected_download
            events = asyncio.run(second_repository._load_calendar_events())

            self.assertFalse(download_called)
            self.assertEqual([event.summary for event in events], ["磁盘缓存直播"])

    def test_is_same_day_handles_cross_day_event(self) -> None:
        event = CalendarEvent(
            summary="跨夜直播",
            description="",
            location="",
            categories="直播",
            url="",
            status="CONFIRMED",
            start=datetime(2026, 3, 29, 23, 30, tzinfo=DISPLAY_TZ),
            end=datetime(2026, 3, 30, 1, 0, tzinfo=DISPLAY_TZ),
        )

        self.assertTrue(self.repository._is_same_day(event, date(2026, 3, 30)))

    def test_get_live_events_for_days_groups_requested_range_only(self) -> None:
        events = [
            CalendarEvent(
                summary="周二直播",
                description="",
                location="",
                categories="直播",
                url="",
                status="CONFIRMED",
                start=datetime(2026, 3, 31, 20, 0, tzinfo=DISPLAY_TZ),
                end=datetime(2026, 3, 31, 21, 0, tzinfo=DISPLAY_TZ),
            ),
            CalendarEvent(
                summary="周三直播",
                description="",
                location="",
                categories="直播",
                url="",
                status="CONFIRMED",
                start=datetime(2026, 4, 1, 20, 0, tzinfo=DISPLAY_TZ),
                end=datetime(2026, 4, 1, 21, 0, tzinfo=DISPLAY_TZ),
            ),
            CalendarEvent(
                summary="周日直播",
                description="",
                location="",
                categories="直播",
                url="",
                status="CONFIRMED",
                start=datetime(2026, 4, 5, 20, 0, tzinfo=DISPLAY_TZ),
                end=datetime(2026, 4, 5, 21, 0, tzinfo=DISPLAY_TZ),
            ),
        ]

        async def fake_load_calendar_events():
            return events

        self.repository._load_calendar_events = fake_load_calendar_events

        grouped = asyncio.run(
            self.repository.get_live_events_for_days(
                date(2026, 4, 1),
                date(2026, 4, 5),
            )
        )

        self.assertEqual(list(grouped), [
            date(2026, 4, 1),
            date(2026, 4, 2),
            date(2026, 4, 3),
            date(2026, 4, 4),
            date(2026, 4, 5),
        ])
        self.assertEqual([event.summary for event in grouped[date(2026, 4, 1)]], ["周三直播"])
        self.assertEqual([event.summary for event in grouped[date(2026, 4, 5)]], ["周日直播"])


class ScheduleServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = ScheduleService()

    def test_build_schedule_items_merges_same_slot_hosts(self) -> None:
        start = datetime(2026, 3, 30, 20, 0, tzinfo=DISPLAY_TZ)
        events = [
            CalendarEvent(
                summary="嘉然直播：晚间歌会",
                description="嘉然 | 嘉然",
                location="",
                categories="歌会",
                url="",
                status="CONFIRMED",
                start=start,
                end=datetime(2026, 3, 30, 21, 0, tzinfo=DISPLAY_TZ),
            ),
            CalendarEvent(
                summary="乃琳直播：晚间歌会",
                description="乃琳 | 乃琳",
                location="",
                categories="歌会",
                url="",
                status="CONFIRMED",
                start=start,
                end=datetime(2026, 3, 30, 21, 0, tzinfo=DISPLAY_TZ),
            ),
        ]

        items = self.service.build_schedule_items(events)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].hosts, ["嘉然", "乃琳"])
        self.assertEqual(items[0].label, "演出")
        self.assertEqual(items[0].content, "晚间歌会")

    def test_extract_hosts_detects_group_alias(self) -> None:
        event = CalendarEvent(
            summary="A-SOUL 今日直播安排",
            description="团播 | A-SOUL",
            location="",
            categories="直播",
            url="",
            status="CONFIRMED",
            start=datetime(2026, 3, 30, 20, 0, tzinfo=DISPLAY_TZ),
            end=datetime(2026, 3, 30, 21, 0, tzinfo=DISPLAY_TZ),
        )

        self.assertEqual(self.service._extract_hosts(event), ["嘉然", "乃琳", "贝拉"])

    def test_filter_schedule_items_excludes_supplementary_members(self) -> None:
        start = datetime(2026, 3, 30, 20, 0, tzinfo=DISPLAY_TZ)
        items = [
            ScheduleItem(
                start=start,
                start_text="20:00",
                hosts=["心宜"],
                hosts_text="心宜",
                content="单人直播",
                label="直播",
            ),
            ScheduleItem(
                start=start,
                start_text="20:00",
                hosts=["嘉然", "思诺"],
                hosts_text="嘉然 / 思诺",
                content="联动直播",
                label="联动",
            ),
            ScheduleItem(
                start=start,
                start_text="20:00",
                hosts=[],
                hosts_text="待确认",
                content="待定直播",
                label="直播",
            ),
        ]

        filtered = self.service.exclude_hosts(items, {"心宜", "思诺"})

        self.assertEqual([item.content for item in filtered], ["联动直播", "待定直播"])
        self.assertEqual(filtered[0].hosts, ["嘉然"])
        self.assertEqual(filtered[0].hosts_text, "嘉然")
        self.assertEqual(items[1].hosts, ["嘉然", "思诺"])


class LiveRequestParserTest(unittest.TestCase):
    def test_parse_live_request_accepts_filter_flag_for_all_schedule_commands(self) -> None:
        for command in ("今日直播", "明日直播", "本周直播"):
            with self.subTest(command=command):
                self.assertEqual(parse_live_request(f"{command} -a"), (command, True))

    def test_parse_live_request_preserves_commands_without_filter_flag(self) -> None:
        self.assertEqual(parse_live_request("今日直播"), ("今日直播", False))

    def test_parse_live_request_rejects_unknown_or_extra_options(self) -> None:
        self.assertIsNone(parse_live_request("今日直播 -x"))
        self.assertIsNone(parse_live_request("今日直播 -a extra"))


if __name__ == "__main__":
    unittest.main()
