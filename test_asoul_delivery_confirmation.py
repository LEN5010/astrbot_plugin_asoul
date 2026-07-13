import asyncio
import copy
import unittest

from asoul_bilibili import (
    BilibiliPlannedNotification,
    BilibiliUidDeliveryPlan,
    BilibiliUidSnapshot,
)
from test_asoul_push_targets import _load_main_module


class RecordingContext:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_rules: dict[tuple[str, int], Exception] = {}
        self.origin_counts: dict[str, int] = {}

    async def send_message(self, origin, result):
        count = self.origin_counts.get(origin, 0) + 1
        self.origin_counts[origin] = count
        self.sent.append(origin)
        failure = self.fail_rules.get((origin, count))
        if failure is not None:
            raise failure
        return result

    def get_platform_inst(self, *args, **kwargs):
        return None


class FakeMonitor:
    def __init__(self, main_module) -> None:
        self.main = main_module
        self.fetch_calls: list[dict] = []
        self.plan_inputs: list[dict] = []

    async def fetch_uid_snapshot(self, config, uid, previous_state=None):
        self.fetch_calls.append(
            {
                "uid": uid,
                "previous_state": copy.deepcopy(previous_state),
            }
        )
        return BilibiliUidSnapshot(
            uid=uid,
            author_name="测试账号",
        )

    def plan_uid_deliveries(self, config, previous_state, snapshot):
        self.plan_inputs.append(copy.deepcopy(previous_state or {}))
        state_1 = {
            "author_name": snapshot.author_name,
            "last_dynamic_id": "dyn-1",
            "recent_dynamic_ids": ["dyn-1"],
        }
        state_2 = {
            "author_name": snapshot.author_name,
            "last_dynamic_id": "dyn-2",
            "recent_dynamic_ids": ["dyn-2", "dyn-1"],
        }
        deliveries = [
            BilibiliPlannedNotification(
                notification=self.main.BilibiliNotification(
                    kind="dynamic",
                    uid=snapshot.uid,
                    author_name=snapshot.author_name,
                    title="",
                    url="https://t.bilibili.com/dyn-1",
                    text="第一条",
                ),
                uid_state=state_1,
            ),
            BilibiliPlannedNotification(
                notification=self.main.BilibiliNotification(
                    kind="dynamic",
                    uid=snapshot.uid,
                    author_name=snapshot.author_name,
                    title="",
                    url="https://t.bilibili.com/dyn-2",
                    text="第二条",
                ),
                uid_state=state_2,
            ),
        ]
        return BilibiliUidDeliveryPlan(
            deliveries=deliveries,
            final_state=state_2,
        )


class ASoulDeliveryConfirmationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.main = _load_main_module()

    def _new_plugin(self, context: RecordingContext, group_whitelist: list[str]):
        plugin = self.main.ASoulPlugin(
            context,
            config={
                "enabled": False,
                "group_whitelist": list(group_whitelist),
                "target_uids": ["100"],
            },
        )
        plugin._bilibili_monitor = FakeMonitor(self.main)
        return plugin

    def test_single_target_confirms_only_after_each_successful_send(self) -> None:
        context = RecordingContext()
        origin = "aiocqhttp:GroupMessage:100"
        context.fail_rules[(origin, 2)] = RuntimeError("second send failed")
        plugin = self._new_plugin(context, ["100"])
        plugin._bilibili_push_targets = {
            origin: {
                "group_id": "100",
                "platform_name": "aiocqhttp",
                "unified_msg_origin": origin,
            }
        }

        asyncio.run(plugin._poll_bilibili_updates_for_uid("100"))

        target_state = plugin._bilibili_monitor_state["targets"][origin]["uids"]["100"]
        self.assertEqual(target_state["last_dynamic_id"], "dyn-1")
        self.assertEqual(target_state["recent_dynamic_ids"], ["dyn-1"])

    def test_targets_confirm_independently_when_one_group_fails(self) -> None:
        context = RecordingContext()
        origin_ok = "aiocqhttp:GroupMessage:100"
        origin_fail = "aiocqhttp:GroupMessage:200"
        context.fail_rules[(origin_fail, 1)] = RuntimeError("group send failed")
        plugin = self._new_plugin(context, ["100", "200"])
        plugin._bilibili_push_targets = {
            origin_ok: {
                "group_id": "100",
                "platform_name": "aiocqhttp",
                "unified_msg_origin": origin_ok,
            },
            origin_fail: {
                "group_id": "200",
                "platform_name": "aiocqhttp",
                "unified_msg_origin": origin_fail,
            },
        }

        asyncio.run(plugin._poll_bilibili_updates_for_uid("100"))

        ok_state = plugin._bilibili_monitor_state["targets"][origin_ok]["uids"]["100"]
        fail_state = plugin._bilibili_monitor_state["targets"][origin_fail]["uids"].get("100", {})
        self.assertEqual(ok_state["last_dynamic_id"], "dyn-2")
        self.assertEqual(fail_state, {})

    def test_no_active_targets_does_not_fetch_or_advance_state(self) -> None:
        context = RecordingContext()
        plugin = self._new_plugin(context, ["999"])

        asyncio.run(plugin._poll_bilibili_updates_for_uid("100"))

        self.assertEqual(plugin._bilibili_monitor.fetch_calls, [])
        self.assertEqual(plugin._bilibili_monitor_state, {})

    def test_persist_failure_does_not_block_other_targets_or_memory_state(self) -> None:
        context = RecordingContext()
        origin_a = "aiocqhttp:GroupMessage:100"
        origin_b = "aiocqhttp:GroupMessage:200"
        plugin = self._new_plugin(context, ["100", "200"])
        plugin._bilibili_push_targets = {
            origin_a: {
                "group_id": "100",
                "platform_name": "aiocqhttp",
                "unified_msg_origin": origin_a,
            },
            origin_b: {
                "group_id": "200",
                "platform_name": "aiocqhttp",
                "unified_msg_origin": origin_b,
            },
        }

        original_put_kv_data = plugin.put_kv_data
        persist_calls = {"count": 0}

        async def flaky_put_kv_data(key, value):
            if key == self.main.KV_BILIBILI_MONITOR_STATE:
                persist_calls["count"] += 1
                if persist_calls["count"] == 1:
                    raise RuntimeError("kv store unavailable")
            return await original_put_kv_data(key, value)

        plugin.put_kv_data = flaky_put_kv_data

        asyncio.run(plugin._poll_bilibili_updates_for_uid("100"))

        self.assertEqual(context.origin_counts[origin_a], 2)
        self.assertEqual(context.origin_counts[origin_b], 2)
        state_a = plugin._bilibili_monitor_state["targets"][origin_a]["uids"]["100"]
        state_b = plugin._bilibili_monitor_state["targets"][origin_b]["uids"]["100"]
        self.assertEqual(state_a["last_dynamic_id"], "dyn-2")
        self.assertEqual(state_b["last_dynamic_id"], "dyn-2")


if __name__ == "__main__":
    unittest.main()
