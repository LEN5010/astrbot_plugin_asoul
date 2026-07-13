import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

from test_asoul_push_targets import _install_astrbot_stubs, _load_main_module

_install_astrbot_stubs()

from asoul_bilibili_runtime import COMMENT_POLL_STATE_KEY, BilibiliRuntime


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
        return plugin, plugin._bilibili_runtime

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

    def test_persisted_attempt_delays_first_poll_after_restart(self) -> None:
        _, runtime = self._new_runtime()
        runtime.push_config = replace(
            runtime.push_config,
            enabled=True,
            push_comment=True,
            target_uids=["100"],
        )
        runtime.refresh_config = lambda: None
        runtime.gateway.has_credential = lambda: True
        runtime.monitor_state = {
            "targets": {},
            "bootstrap_uids": {},
            COMMENT_POLL_STATE_KEY: {
                "100": {
                    "last_attempt_at": NOW_TS - 10,
                    "last_success_at": NOW_TS - 10,
                    "last_result": "success",
                }
            },
        }
        poll_calls: list[str] = []
        sleep_delays: list[float] = []

        async def record_poll(uid: str) -> None:
            poll_calls.append(uid)

        async def stop_on_sleep(delay: float) -> None:
            sleep_delays.append(delay)
            raise asyncio.CancelledError

        runtime.poll_bilibili_comments_for_uid = record_poll
        with patch("asoul_bilibili_runtime.time.time", return_value=NOW_TS), patch(
            "asoul_bilibili_runtime.time.monotonic", return_value=100.0
        ), patch("asoul_bilibili_runtime.asyncio.sleep", new=stop_on_sleep):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(runtime._run_comment_monitor_loop())

        self.assertEqual(poll_calls, [])
        self.assertEqual(sleep_delays, [2.0])

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

    def test_status_text_reports_comment_runtime_state(self) -> None:
        _, runtime = self._new_runtime()
        runtime._runtime_initialized = True
        runtime.monitor_state = {
            "targets": {},
            "bootstrap_uids": {},
            COMMENT_POLL_STATE_KEY: {
                "100": {
                    "last_attempt_at": NOW_TS,
                    "last_success_at": 0,
                    "last_result": "error",
                    "last_error": {
                        "at": NOW_TS,
                        "category": "risk_control",
                        "code": "412",
                        "message": "请求被 B 站风控拒绝",
                    }
                }
            },
        }

        text = asyncio.run(runtime.build_bilibili_status_text())

        self.assertIn("【B站评论推送状态】", text)
        self.assertIn("UID 100", text)
        self.assertIn("risk_control/412", text)
        self.assertIn("请求被 B 站风控拒绝", text)

    def test_update_and_comment_poll_for_same_uid_do_not_overlap(self) -> None:
        _, runtime = self._new_runtime()
        active = 0
        max_active = 0

        async def record_concurrency(_uid: str) -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0)
            active -= 1

        runtime._poll_bilibili_updates_for_uid = record_concurrency
        runtime._poll_bilibili_comments_for_uid = record_concurrency

        async def run_both() -> None:
            await asyncio.gather(
                runtime.poll_bilibili_updates_for_uid("100"),
                runtime.poll_bilibili_comments_for_uid("100"),
            )

        asyncio.run(run_both())

        self.assertEqual(max_active, 1)

    def test_bili_status_command_is_thin_runtime_wrapper(self) -> None:
        plugin, _ = self._new_runtime()

        class Event:
            @staticmethod
            def plain_result(text: str) -> str:
                return text

        async def collect_results() -> list[str]:
            return [item async for item in plugin.bili_status(Event())]

        results = asyncio.run(collect_results())

        self.assertEqual(len(results), 1)
        self.assertIn("【B站评论推送状态】", results[0])


if __name__ == "__main__":
    unittest.main()
