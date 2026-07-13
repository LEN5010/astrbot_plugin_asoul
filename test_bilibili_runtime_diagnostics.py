import asyncio
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from test_asoul_push_targets import _install_astrbot_stubs, _load_main_module

_install_astrbot_stubs()

from asoul_bilibili import BilibiliGateway
from asoul_bilibili_runtime import (
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
