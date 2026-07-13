import asyncio
import unittest
from datetime import date, datetime

from asoul_calendar import CalendarRepository
from asoul_core import DISPLAY_TZ, CalendarEvent
from asoul_schedule import ScheduleService


class CalendarRepositoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = CalendarRepository()

    def test_parse_calendar_decodes_multiline_event(self) -> None:
        ics_text = "\n".join(
            [
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "DTSTART;TZID=Asia/Shanghai:20260330T200000",
                "DTEND;TZID=Asia/Shanghai:20260330T213000",
                "SUMMARY:嘉然直播：晚间歌会",
                "DESCRIPTION:嘉然 | 嘉然\\n唱歌专场",
                "LOCATION:B站直播间",
                "CATEGORIES:歌会",
                "URL:https://live.bilibili.com/12345",
                "STATUS:CONFIRMED",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        )

        events = self.repository._parse_calendar(ics_text)

        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.summary, "嘉然直播：晚间歌会")
        self.assertEqual(event.description, "嘉然 | 嘉然\n唱歌专场")
        self.assertEqual(event.start, datetime(2026, 3, 30, 20, 0, tzinfo=DISPLAY_TZ))
        self.assertTrue(self.repository._is_livestream_event(event))

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


if __name__ == "__main__":
    unittest.main()
