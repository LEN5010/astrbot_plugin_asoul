import asyncio
import unittest
from datetime import datetime

from asoul_core import DISPLAY_TZ, ScheduleItem
from asoul_schedule import ScheduleService
from asoul_schedule_highlight import (
    KV_SCHEDULE_HIGHLIGHTS,
    ScheduleHighlightManager,
    build_schedule_highlight_key,
)


def schedule_item(
    *,
    day: int = 25,
    hour: int = 20,
    content: str = "测试节目",
) -> ScheduleItem:
    return ScheduleItem(
        start=datetime(2099, 7, day, hour, 0, tzinfo=DISPLAY_TZ),
        start_text=f"{hour:02d}:00",
        hosts=["贝拉"],
        hosts_text="贝拉",
        content=content,
        label="直播",
    )


class FakeOwner:
    def __init__(self) -> None:
        self.store = {}

    async def get_kv_data(self, key, default):
        return self.store.get(key, default)

    async def put_kv_data(self, key, value):
        self.store[key] = value


class ScheduleHighlightTest(unittest.TestCase):
    def test_key_is_stable_and_distinguishes_schedule_identity(self) -> None:
        first = schedule_item()
        same = schedule_item()
        moved = schedule_item(hour=21)

        self.assertEqual(
            build_schedule_highlight_key(first),
            build_schedule_highlight_key(same),
        )
        self.assertNotEqual(
            build_schedule_highlight_key(first),
            build_schedule_highlight_key(moved),
        )

    def test_global_highlight_persists_and_can_be_removed(self) -> None:
        owner = FakeOwner()
        item = schedule_item()

        async def exercise():
            manager = ScheduleHighlightManager(owner)
            record = await manager.mark(item)
            highlighted = await manager.apply([item])

            restored = ScheduleHighlightManager(owner)
            restored_items = await restored.apply([item])
            removed = await restored.unmark(item)
            plain_items = await restored.apply([item])
            return record, highlighted, restored_items, removed, plain_items

        record, highlighted, restored_items, removed, plain_items = asyncio.run(
            exercise()
        )

        self.assertEqual(record.content, "测试节目")
        self.assertTrue(highlighted[0].highlighted)
        self.assertTrue(restored_items[0].highlighted)
        self.assertTrue(removed)
        self.assertFalse(plain_items[0].highlighted)
        self.assertIn("records", owner.store[KV_SCHEDULE_HIGHLIGHTS])

    def test_text_fallback_marks_special_attention(self) -> None:
        item = schedule_item()
        item.highlighted = True

        text = ScheduleService().format_schedule_fallback(
            [item], item.start.date(), "今日直播"
        )

        self.assertIn("⭐【特别关注】20:00 贝拉 测试节目", text)

    def test_old_records_are_retained_until_explicitly_removed(self) -> None:
        owner = FakeOwner()
        owner.store[KV_SCHEDULE_HIGHLIGHTS] = {
            "version": 1,
            "records": {
                "expired": {
                    "key": "expired",
                    "target_date": "2000-01-01",
                    "start_text": "20:00",
                    "content": "旧节目",
                    "hosts_text": "贝拉",
                    "created_at": 1,
                }
            },
        }
        owner.store["unrelated"] = {"keep": True}

        manager = ScheduleHighlightManager(owner)
        records = asyncio.run(manager.list_records())

        self.assertEqual([record.key for record in records], ["expired"])
        self.assertIn(
            "expired", owner.store[KV_SCHEDULE_HIGHLIGHTS]["records"]
        )
        self.assertEqual(owner.store["unrelated"], {"keep": True})


if __name__ == "__main__":
    unittest.main()
