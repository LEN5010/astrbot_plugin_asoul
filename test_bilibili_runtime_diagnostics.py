import asyncio
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from test_asoul_push_targets import _install_astrbot_stubs, _load_main_module

_install_astrbot_stubs()

from asoul_bilibili import BilibiliGateway
from asoul_bilibili_runtime import (
    CONTENT_POLL_STATE_KEY,
    COMMENT_POLL_STATE_KEY,
    COMMENT_RESOURCE_CATALOGS_KEY,
    BilibiliRuntime,
)


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
        )
        runtime.push_targets = {}
        calls: list[str] = []

        async def record_catalog(now: int) -> bool:
            calls.append("catalog")
            return True

        runtime.refresh_one_due_comment_catalog = record_catalog
        worked = asyncio.run(runtime.run_one_comment_work_item(NOW_TS))

        self.assertTrue(worked)
        self.assertEqual(calls, ["catalog"])

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

    def test_failed_poll_persists_structured_error_without_touching_target_state(self) -> None:
        _, runtime = self._new_runtime()
        runtime.monitor_state = {
            "targets": {
                "origin": {
                    "uids": {
                        "100": {
                            "comment_resources": {
                                "video:1": {"recent_comment_ids": ["9001"]}
                            }
                        }
                    }
                }
            },
            "bootstrap_uids": {},
            COMMENT_POLL_STATE_KEY: {},
        }
        before_target_state = runtime.monitor_state["targets"]["origin"]["uids"]["100"]

        asyncio.run(runtime.begin_comment_poll_attempt("100", NOW_TS))
        asyncio.run(
            runtime.finish_comment_poll_attempt(
                "100",
                finished_at=NOW_TS + 1,
                error=runtime.classify_poll_error(FakeRiskControlError()),
            )
        )

        entry = runtime.monitor_state[COMMENT_POLL_STATE_KEY]["100"]
        self.assertEqual(entry["last_attempt_at"], NOW_TS)
        self.assertEqual(entry["last_result"], "error")
        self.assertEqual(entry["last_error"]["category"], "risk_control")
        self.assertEqual(
            runtime.monitor_state["targets"]["origin"]["uids"]["100"],
            before_target_state,
        )

    def test_failed_resource_discovery_preserves_persisted_catalog(self) -> None:
        _, runtime = self._new_runtime()
        runtime.monitor_state = {
            "targets": {},
            "bootstrap_uids": {},
            COMMENT_POLL_STATE_KEY: {},
            COMMENT_RESOURCE_CATALOGS_KEY: {
                "100": {
                    "last_attempt_at": NOW_TS - 601,
                    "last_success_at": NOW_TS - 700,
                    "author_name": "测试账号",
                    "resources": [
                        {
                            "key": "video:2003",
                            "owner_uid": "100",
                            "owner_name": "测试账号",
                            "resource_kind": "video",
                            "oid": 2003,
                            "type_value": 1,
                            "title": "第三个视频",
                            "url": "https://www.bilibili.com/video/BV3",
                        }
                    ],
                }
            },
        }

        async def fail_discovery(uid: str, author_name: str):
            raise FakeRiskControlError()

        runtime.monitor.discover_comment_resources = fail_discovery
        with patch("asoul_bilibili_runtime.time.time", return_value=NOW_TS):
            author_name, resources = asyncio.run(
                runtime.ensure_comment_resource_catalog("100", "测试账号")
            )

        self.assertEqual(author_name, "测试账号")
        self.assertEqual([resource.key for resource in resources], ["video:2003"])
        entry = runtime.monitor_state[COMMENT_RESOURCE_CATALOGS_KEY]["100"]
        self.assertEqual(entry["last_attempt_at"], NOW_TS)
        self.assertEqual(entry["last_success_at"], NOW_TS - 700)
        self.assertEqual(entry["resources"][0]["key"], "video:2003")

    def test_comment_poll_does_not_block_content_poll_for_same_uid(self) -> None:
        _, runtime = self._new_runtime()
        comment_started = asyncio.Event()
        release_comment = asyncio.Event()
        content_started = asyncio.Event()

        async def block_comment(_uid: str) -> None:
            comment_started.set()
            await release_comment.wait()

        async def record_content(_uid: str) -> None:
            content_started.set()

        runtime._poll_bilibili_updates_for_uid = record_content
        runtime._poll_bilibili_comments_for_uid = block_comment

        async def run_both() -> bool:
            comment_task = asyncio.create_task(
                runtime.poll_bilibili_comments_for_uid("100")
            )
            await comment_started.wait()
            content_task = asyncio.create_task(
                runtime.poll_bilibili_updates_for_uid("100")
            )
            try:
                try:
                    await asyncio.wait_for(content_started.wait(), timeout=0.05)
                except TimeoutError:
                    pass
                return content_started.is_set()
            finally:
                release_comment.set()
                await asyncio.gather(comment_task, content_task)

        content_started_before_release = asyncio.run(run_both())

        self.assertTrue(content_started_before_release)

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

    def test_domain_commits_preserve_other_uid_state_fields(self) -> None:
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

        async def commit_both_domains() -> None:
            await asyncio.gather(
                runtime.commit_target_uid_state(
                    origin,
                    "100",
                    {"author_name": "新昵称", "last_dynamic_id": "dyn-2"},
                ),
                runtime.commit_target_uid_state(
                    origin,
                    "100",
                    {
                        "author_name": "新昵称",
                        "comment_resources": {
                            "video:1": {
                                "initialized": True,
                                "last_comment_id": "9001",
                            }
                        },
                    },
                ),
            )

        asyncio.run(commit_both_domains())

        state = runtime.monitor_state["targets"][origin]["uids"]["100"]
        self.assertEqual(state.get("last_dynamic_id"), "dyn-2")
        self.assertEqual(
            state["comment_resources"]["video:1"]["last_comment_id"],
            "9001",
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

        async def exercise() -> tuple[asyncio.Task, asyncio.Task]:
            content_task = asyncio.create_task(asyncio.Event().wait())
            profile_task = asyncio.create_task(asyncio.Event().wait())
            runtime._content_poll_tasks["100"] = content_task
            runtime._profile_refresh_tasks["100"] = profile_task
            await runtime.terminate()
            return content_task, profile_task

        content_task, profile_task = asyncio.run(exercise())

        self.assertTrue(content_task.cancelled())
        self.assertTrue(profile_task.cancelled())
        self.assertEqual(runtime._content_poll_tasks, {})
        self.assertEqual(runtime._profile_refresh_tasks, {})
        self.assertTrue(cleanup_called)

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
