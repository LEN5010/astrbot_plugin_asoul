import asyncio
import time
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from test_asoul_push_targets import _install_astrbot_stubs, _load_main_module

_install_astrbot_stubs()

from asoul_bilibili import BilibiliGateway
from asoul_bilibili_runtime import (
    CONTENT_POLL_STATE_KEY,
    BilibiliRuntime,
)
from asoul_comment_journal import CommentJournalStatus


NOW_TS = 1_700_000_000


class FakeRiskControlError(Exception):
    status = 412


class FakeCredentialError(Exception):
    code = -101


class BilibiliRuntimeDiagnosticsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.main = _load_main_module()

    def _new_runtime(self):
        plugin = self.main.ASoulPlugin(
            self.main.Context(),
            config={
                "enabled": False,
                "push_comment": True,
                "group_whitelist": [],
                "target_uids": ["100"],
            },
        )
        self.addCleanup(lambda: asyncio.run(plugin.terminate()))
        return plugin, plugin._bilibili_runtime

    def test_runtime_uses_plugin_data_directory_for_comment_database(self) -> None:
        plugin, runtime = self._new_runtime()

        self.assertEqual(runtime.comment_journal.path.name, "bilibili_comments.sqlite3")
        self.assertNotIn(
            "plugins/astrbot_plugin_asoul", str(runtime.comment_journal.path)
        )
        asyncio.run(plugin.terminate())

    def test_comment_work_continues_without_active_groups_to_prevent_replay(
        self,
    ) -> None:
        _, runtime = self._new_runtime()
        runtime.push_config = replace(
            runtime.push_config,
            enabled=True,
            push_comment=True,
            target_uids=["100"],
            comment_target_uids=["200"],
        )
        runtime.push_targets = {}
        calls: list[str] = []

        async def no_delivery(send, now: int) -> bool:
            return False

        async def record_scan(task, target_uids, target_origins, now) -> None:
            calls.append("scan")

        runtime.comment_capture.deliver_one = no_delivery
        runtime.comment_capture.run_scan_task = record_scan
        selected_uids: list[list[str]] = []
        runtime.comment_scheduler.next_task = lambda journal, now, uids: (
            selected_uids.append(list(uids))
            or SimpleNamespace(owner_uid="200")
        )
        worked = asyncio.run(runtime.run_one_comment_work_item(NOW_TS))

        self.assertTrue(worked)
        self.assertEqual(calls, ["scan"])
        self.assertEqual(selected_uids, [["200"]])

    def test_terminate_closes_comment_journal(self) -> None:
        plugin, runtime = self._new_runtime()
        journal = runtime.comment_journal
        asyncio.run(plugin.terminate())

        with self.assertRaises(Exception):
            journal.pending_delivery_count()

    def test_poll_error_classification_omits_html_response_body(self) -> None:
        risk_error = FakeRiskControlError("<!DOCTYPE html>blocked")
        credential_error = FakeCredentialError("not logged in")

        risk = BilibiliRuntime.classify_poll_error(risk_error)
        credential = BilibiliRuntime.classify_poll_error(credential_error)

        self.assertEqual(risk.category, "risk_control")
        self.assertEqual(risk.code, "412")
        self.assertEqual(risk.message, "请求被 B 站风控拒绝")
        self.assertNotIn("DOCTYPE", risk.message)
        self.assertEqual(credential.category, "credential")
        self.assertEqual(credential.code, "-101")

    def test_status_text_reports_journal_backlog_and_incomplete_resources(
        self,
    ) -> None:
        _, runtime = self._new_runtime()
        runtime.comment_journal.status = lambda now: CommentJournalStatus(
            lifecycle_counts={"bootstrapping": 1, "active": 5, "retired": 2},
            incomplete_count=1,
            pending_scan_count=4,
            overdue_scan_count=2,
            retrying_scan_count=1,
            oldest_scan_due_at=NOW_TS - 600,
            pending_delivery_count=3,
            oldest_delivery_due_at=NOW_TS - 60,
            last_reconciliation_at=NOW_TS - 1200,
            lane_due_counts={"head": 2, "reply": 1, "reconcile": 1},
            dormant_reply_count=20,
            reply_change_pending_count=1,
            reply_continuation_count=1,
            reply_retrying_count=1,
            baseline_pending_count=1,
            oldest_head_due_at=NOW_TS - 600,
            last_root_reconciliation_at=NOW_TS - 1200,
            last_reply_reconciliation_at=NOW_TS - 600,
            request_count_15m=15,
            request_count_60m=60,
            reply_safety_interval_seconds=48 * 60 * 60,
            owner_last_attempt_at={"100": NOW_TS - 10},
        )

        text = asyncio.run(runtime.build_bilibili_status_text())

        self.assertIn("活跃资源：5", text)
        self.assertIn("不完整资源：1", text)
        self.assertIn("头部待处理：2", text)
        self.assertIn(
            "回复任务：变化待核对 1；分页中 1；重试 1；当前到期 1",
            text,
        )
        self.assertIn("休眠楼层：20", text)
        self.assertIn("根索引待处理：1", text)
        self.assertIn("评论请求吞吐：15 分钟 15；60 分钟 60", text)
        self.assertIn("最近根评论完整核对", text)
        self.assertIn("最近楼中楼完整核对", text)
        self.assertIn("待投递：3", text)
        self.assertIn("内容监控 UID：1", text)
        self.assertIn("评论监控 UID：1", text)
        self.assertIn("评论请求最小间隔：2 秒", text)
        self.assertIn("安全复查工作量超过单日容量", text)


    def test_slow_content_uid_does_not_block_next_uid(self) -> None:
        _, runtime = self._new_runtime()
        runtime.push_config = replace(
            runtime.push_config,
            enabled=True,
            target_uids=["100", "200"],
            task_gap_seconds=0.0,
        )
        runtime.refresh_config = lambda: None
        runtime.gateway.has_credential = lambda: True
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()

        async def poll(uid: str) -> None:
            if uid == "100":
                first_started.set()
                await release_first.wait()
                return
            second_started.set()

        runtime.poll_bilibili_updates_for_uid = poll

        async def run_loop() -> None:
            loop_task = asyncio.create_task(runtime._run_monitor_loop())
            await first_started.wait()
            try:
                try:
                    await asyncio.wait_for(second_started.wait(), timeout=0.05)
                except TimeoutError:
                    pass
            finally:
                release_first.set()
                loop_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await loop_task

        asyncio.run(run_loop())

        self.assertTrue(second_started.is_set())

    def test_content_commit_preserves_legacy_comment_state(self) -> None:
        _, runtime = self._new_runtime()
        origin = "aiocqhttp:GroupMessage:100"
        runtime.monitor_state = {
            "targets": {
                origin: {
                    "uids": {
                        "100": {
                            "author_name": "旧昵称",
                            "last_dynamic_id": "dyn-1",
                            "comment_resources": {"video:1": {"initialized": True}},
                        }
                    }
                }
            },
            "bootstrap_uids": {},
        }

        asyncio.run(
            runtime.commit_target_uid_state(
                origin,
                "100",
                {"author_name": "新昵称", "last_dynamic_id": "dyn-2"},
            )
        )

        state = runtime.monitor_state["targets"][origin]["uids"]["100"]
        self.assertEqual(state.get("last_dynamic_id"), "dyn-2")
        self.assertEqual(
            state["comment_resources"]["video:1"],
            {"initialized": True},
        )

    def test_content_poll_attempt_records_success_and_duration(self) -> None:
        _, runtime = self._new_runtime()

        async def succeed(_uid: str) -> None:
            await asyncio.sleep(0.01)

        runtime.poll_bilibili_updates_for_uid = succeed
        with patch(
            "asoul_bilibili_runtime.time.time",
            side_effect=[NOW_TS, NOW_TS + 3],
        ):
            asyncio.run(runtime._run_content_poll_attempt("100"))

        entry = runtime.monitor_state.get(CONTENT_POLL_STATE_KEY, {}).get("100", {})
        self.assertEqual(entry.get("last_attempt_at"), NOW_TS)
        self.assertEqual(entry.get("last_success_at"), NOW_TS + 3)
        self.assertEqual(entry.get("last_result"), "success")
        self.assertGreaterEqual(entry.get("last_duration_ms", 0), 5)

    def test_cancelled_content_poll_is_not_recorded_as_success(self) -> None:
        _, runtime = self._new_runtime()
        poll_started = asyncio.Event()

        async def wait_forever(_uid: str) -> None:
            poll_started.set()
            await asyncio.Event().wait()

        runtime.poll_bilibili_updates_for_uid = wait_forever

        async def run_and_cancel() -> None:
            task = asyncio.create_task(runtime._run_content_poll_attempt("100"))
            await poll_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(run_and_cancel())

        entry = runtime.monitor_state.get(CONTENT_POLL_STATE_KEY, {}).get("100", {})
        self.assertEqual(entry.get("last_result"), "cancelled")
        self.assertEqual(entry.get("last_success_at", 0), 0)

    def test_content_poll_timeout_is_recorded_without_success(self) -> None:
        _, runtime = self._new_runtime()

        async def wait_forever(_uid: str) -> None:
            await asyncio.Event().wait()

        runtime.poll_bilibili_updates_for_uid = wait_forever
        with patch("asoul_bilibili_runtime.CONTENT_POLL_TIMEOUT_SECONDS", 0.01):
            asyncio.run(runtime._run_content_poll_attempt("100"))

        entry = runtime.monitor_state.get(CONTENT_POLL_STATE_KEY, {}).get("100", {})
        self.assertEqual(entry.get("last_result"), "timeout")
        self.assertEqual(entry.get("last_success_at", 0), 0)
        self.assertEqual(entry.get("last_error", {}).get("category"), "timeout")

    def test_status_reports_content_poll_runtime(self) -> None:
        _, runtime = self._new_runtime()
        runtime.monitor_state = {
            "targets": {},
            "bootstrap_uids": {},
            CONTENT_POLL_STATE_KEY: {
                "100": {
                    "last_attempt_at": NOW_TS,
                    "last_success_at": NOW_TS + 2,
                    "last_result": "success",
                    "last_duration_ms": 2100,
                }
            },
        }
        runtime._runtime_initialized = True

        status = asyncio.run(runtime.build_bilibili_status_text())

        self.assertIn("内容任务", status)
        self.assertIn("耗时 2.10 秒", status)

    def test_terminate_cancels_inflight_content_and_profile_tasks(self) -> None:
        _, runtime = self._new_runtime()
        cleanup_called = False

        class FakeRenderer:
            async def cleanup(self) -> None:
                nonlocal cleanup_called
                cleanup_called = True

        runtime.card_renderer = FakeRenderer()

        async def exercise() -> tuple[asyncio.Task, asyncio.Task, asyncio.Task, asyncio.Task]:
            content_task = asyncio.create_task(asyncio.Event().wait())
            profile_task = asyncio.create_task(asyncio.Event().wait())
            comment_task = asyncio.create_task(asyncio.Event().wait())
            catalog_task = asyncio.create_task(asyncio.Event().wait())
            runtime._content_poll_tasks["100"] = content_task
            runtime._profile_refresh_tasks["100"] = profile_task
            runtime.comment_task = comment_task
            runtime.comment_catalog_task = catalog_task
            await runtime.terminate()
            return content_task, profile_task, comment_task, catalog_task

        content_task, profile_task, comment_task, catalog_task = asyncio.run(exercise())

        self.assertTrue(content_task.cancelled())
        self.assertTrue(profile_task.cancelled())
        self.assertTrue(comment_task.cancelled())
        self.assertTrue(catalog_task.cancelled())
        self.assertEqual(runtime._content_poll_tasks, {})
        self.assertEqual(runtime._profile_refresh_tasks, {})
        self.assertTrue(cleanup_called)

    def test_comment_catalog_timeout_does_not_create_resource_lifecycle(self) -> None:
        _, runtime = self._new_runtime()
        runtime.push_config = replace(
            runtime.push_config,
            enabled=True,
            push_comment=True,
            target_uids=["100"],
        )

        async def wait_forever(uid: str) -> str:
            await asyncio.Event().wait()

        runtime.gateway.get_comment_resource_owner_name = wait_forever
        with patch(
            "asoul_bilibili_runtime.COMMENT_CATALOG_TIMEOUT_SECONDS", 0.01
        ):
            worked = asyncio.run(
                runtime.refresh_one_due_comment_catalog(NOW_TS)
            )

        lifecycle_count = runtime.comment_journal._connection.execute(
            "SELECT COUNT(*) FROM resource_lifecycle"
        ).fetchone()[0]
        owner = runtime.comment_journal._connection.execute(
            """
            SELECT last_error_category, retry_count, next_attempt_at
            FROM owner_catalog
            WHERE owner_uid = '100'
            """
        ).fetchone()
        self.assertTrue(worked)
        self.assertEqual(lifecycle_count, 0)
        self.assertEqual(owner["last_error_category"], "network")
        self.assertEqual(owner["retry_count"], 1)
        self.assertGreaterEqual(owner["next_attempt_at"], NOW_TS + 60)
        self.assertLessEqual(owner["next_attempt_at"], NOW_TS + 43_200)

    def test_comment_gateway_spaces_all_monitor_requests(self) -> None:
        gateway = BilibiliGateway(comment_request_interval_seconds=2.0)
        calls: list[str] = []
        sleeps: list[float] = []

        async def fake_get_user_name(uid: str) -> str:
            calls.append(uid)
            return "测试账号"

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        gateway.get_user_name = fake_get_user_name
        gateway._next_comment_request_at = time.monotonic() + 2.0
        with patch("asoul_bilibili.asyncio.sleep", new=fake_sleep):
            asyncio.run(gateway.get_comment_resource_owner_name("100"))

        self.assertEqual(calls, ["100"])
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 1.9)


if __name__ == "__main__":
    unittest.main()
