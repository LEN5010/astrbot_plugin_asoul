import asyncio
import importlib.util
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from asoul_bilibili import (
    BilibiliAuthorCardProfile,
    BilibiliEngagementStats,
    BilibiliNotification,
    BilibiliUidDeliveryPlan,
    BilibiliVideoPost,
    KV_BILIBILI_GROUP_ORIGINS,
)
from asoul_core import DISPLAY_TZ, ScheduleItem


def _install_astrbot_stubs() -> None:
    if "astrbot.api.star" in sys.modules:
        return

    def decorator_factory(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    class DummyLogger:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    class DummyMessageEventResult:
        def __init__(self, chain=None):
            self.chain = chain or []

        def use_t2i(self, enabled):
            return self

    class DummyStar:
        def __init__(self, context):
            self.context = context
            self._kv_store = {}

        async def put_kv_data(self, key, value):
            self._kv_store[key] = value

        async def get_kv_data(self, key, default=None):
            return self._kv_store.get(key, default)

        async def delete_kv_data(self, key):
            self._kv_store.pop(key, None)

    class DummyContext:
        async def send_message(self, *args, **kwargs):
            return None

        def get_platform_inst(self, *args, **kwargs):
            return None

    class DummyStarTools:
        @staticmethod
        def get_data_dir(*args, **kwargs):
            return Path(tempfile.mkdtemp(prefix="asoul_plugin_test_"))

    class DummyImage:
        @staticmethod
        def fromFileSystem(path):
            return ("image", path)

        @staticmethod
        def fromURL(url):
            return ("image_url", url)

    message_components_module = types.ModuleType("astrbot.api.message_components")
    message_components_module.AtAll = type("AtAll", (), {})
    message_components_module.Plain = lambda text="": ("plain", text)
    message_components_module.Image = DummyImage

    filter_namespace = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(GROUP_MESSAGE="group", ALL="all"),
        PermissionType=types.SimpleNamespace(ADMIN="admin"),
        on_astrbot_loaded=decorator_factory,
        event_message_type=decorator_factory,
        permission_type=decorator_factory,
        command=decorator_factory,
    )

    event_module = types.ModuleType("astrbot.api.event")
    event_module.AstrMessageEvent = object
    event_module.MessageEventResult = DummyMessageEventResult
    event_module.filter = filter_namespace

    star_module = types.ModuleType("astrbot.api.star")
    star_module.Context = DummyContext
    star_module.Star = DummyStar
    star_module.StarTools = DummyStarTools
    star_module.register = decorator_factory

    api_module = types.ModuleType("astrbot.api")
    api_module.logger = DummyLogger()
    api_module.message_components = message_components_module
    api_module.event = event_module
    api_module.star = star_module

    astrbot_module = types.ModuleType("astrbot")
    astrbot_module.api = api_module

    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.message_components"] = message_components_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.star"] = star_module

    login_v2_module = types.SimpleNamespace(
        QrCodeLogin=lambda platform=None: None,
        QrCodeLoginChannel=types.SimpleNamespace(WEB="web"),
        QrCodeLoginEvents=types.SimpleNamespace(DONE="done", TIMEOUT="timeout"),
    )
    bilibili_api_module = types.ModuleType("bilibili_api")
    bilibili_api_module.login_v2 = login_v2_module
    sys.modules["bilibili_api"] = bilibili_api_module


def _load_main_module():
    _install_astrbot_stubs()
    module_name = "astrbot_plugin_asoul_main_test"
    if module_name in sys.modules:
        return sys.modules[module_name]

    plugin_dir = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(module_name, plugin_dir / "main.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class DummyEvent:
    def __init__(self, group_id: str, unified_msg_origin: str) -> None:
        self.message_obj = types.SimpleNamespace(group_id=group_id)
        self.unified_msg_origin = unified_msg_origin


class ASoulPushTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.main = _load_main_module()
        self.context = self.main.Context()

    def _new_plugin(self, group_whitelist: list[str]):
        plugin = self.main.ASoulPlugin(
            self.context,
            config={
                "enabled": False,
                "group_whitelist": list(group_whitelist),
                "target_uids": ["672328094"],
            },
        )
        self.addCleanup(lambda: asyncio.run(plugin.terminate()))
        return plugin

    def test_registers_multiple_groups_as_independent_targets(self) -> None:
        plugin = self._new_plugin(["100", "200"])

        asyncio.run(
            plugin.remember_group_origin(
                DummyEvent("100", "aiocqhttp:GroupMessage:100")
            )
        )
        asyncio.run(
            plugin.remember_group_origin(
                DummyEvent("200", "aiocqhttp:GroupMessage:200")
            )
        )

        targets = plugin._bilibili_runtime.get_active_push_targets()

        self.assertEqual(
            sorted(target.group_id for target in targets),
            ["100", "200"],
        )
        self.assertEqual(
            sorted(target.unified_msg_origin for target in targets),
            ["aiocqhttp:GroupMessage:100", "aiocqhttp:GroupMessage:200"],
        )

    def test_admin_can_mark_and_unmark_schedule_by_display_index(self) -> None:
        plugin = self._new_plugin([])
        item = ScheduleItem(
            start=datetime(2099, 7, 25, 20, 0, tzinfo=DISPLAY_TZ),
            start_text="20:00",
            hosts=["贝拉"],
            hosts_text="贝拉",
            content="特别节目",
            label="节目",
        )

        async def get_items(_target_date):
            return item.start.date(), await plugin._schedule_highlights.apply(
                [item]
            )

        class CommandEvent:
            @staticmethod
            def plain_result(text):
                return text

        plugin._get_schedule_items_for_admin = get_items

        async def exercise():
            marked = [
                result
                async for result in plugin.highlight_schedule(
                    CommandEvent(), "2099-07-25", 1, "粉色"
                )
            ]
            highlighted = await plugin._schedule_highlights.apply([item])
            removed = [
                result
                async for result in plugin.unhighlight_schedule(
                    CommandEvent(), "2099-07-25", 1
                )
            ]
            plain = await plugin._schedule_highlights.apply([item])
            return marked, highlighted, removed, plain

        marked, highlighted, removed, plain = asyncio.run(exercise())

        self.assertIn("已设为特别关注", marked[0])
        self.assertIn("粉色", marked[0])
        self.assertTrue(highlighted[0].highlighted)
        self.assertEqual(highlighted[0].highlight_style, "pink")
        self.assertIn("已取消特别关注", removed[0])
        self.assertFalse(plain[0].highlighted)

    def test_same_group_replaces_stale_origin(self) -> None:
        plugin = self._new_plugin(["100"])

        asyncio.run(
            plugin.remember_group_origin(
                DummyEvent("100", "aiocqhttp:GroupMessage:100_old")
            )
        )
        asyncio.run(
            plugin.remember_group_origin(
                DummyEvent("100", "aiocqhttp:GroupMessage:100_new")
            )
        )

        targets = plugin._bilibili_runtime.get_active_push_targets()

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].group_id, "100")
        self.assertEqual(targets[0].unified_msg_origin, "aiocqhttp:GroupMessage:100_new")
        self.assertNotIn("aiocqhttp:GroupMessage:100_old", plugin._bilibili_runtime.push_targets)

    def test_runtime_config_refresh_accepts_new_whitelist_group_without_restart(self) -> None:
        plugin = self._new_plugin(["100"])

        asyncio.run(
            plugin.remember_group_origin(
                DummyEvent("100", "aiocqhttp:GroupMessage:100")
            )
        )

        plugin.config["group_whitelist"] = ["100", "200"]
        asyncio.run(
            plugin.remember_group_origin(
                DummyEvent("200", "aiocqhttp:GroupMessage:200")
            )
        )

        targets = plugin._bilibili_runtime.get_active_push_targets()

        self.assertEqual(
            sorted(target.group_id for target in targets),
            ["100", "200"],
        )

    def test_load_runtime_state_normalizes_legacy_group_origin_mapping(self) -> None:
        plugin = self._new_plugin(["100", "200"])
        plugin._kv_store[KV_BILIBILI_GROUP_ORIGINS] = {
            "100": "aiocqhttp:GroupMessage:100",
            "200": "aiocqhttp:GroupMessage:200",
        }

        asyncio.run(plugin._bilibili_runtime.load_state())

        targets = plugin._bilibili_runtime.get_active_push_targets()

        self.assertEqual(
            sorted(target.group_id for target in targets),
            ["100", "200"],
        )
        self.assertIn("aiocqhttp:GroupMessage:100", plugin._bilibili_runtime.push_targets)
        self.assertIn("aiocqhttp:GroupMessage:200", plugin._bilibili_runtime.push_targets)

    def test_comment_notification_parts_render_new_format_with_images(self) -> None:
        plugin = self._new_plugin(["100"])
        notification = BilibiliNotification(
            kind="comment",
            uid="672328094",
            author_name="乃琳Queen",
            title="",
            url="https://www.bilibili.com/video/BV1xx411c7mD",
            text="今天状态很好",
            image_urls=["https://i0.hdslb.com/comment-image.png"],
            comment_created_at=1_700_000_000,
            comment_resource_owner_name="嘉然今天吃什么",
            comment_resource_kind="视频",
            comment_resource_title="鸣潮3.1主线上半！",
            comment_action_text="发表了评论",
        )

        parts = plugin._bilibili_runtime.build_notification_parts(notification)

        self.assertEqual(parts[0][0], "plain")
        self.assertIn("【B站评论】乃琳Queen", parts[0][1])
        self.assertIn("乃琳Queen于2023-11-15 06:13", parts[0][1])
        self.assertIn("在嘉然今天吃什么的视频《鸣潮3.1主线上半！》下发表了评论：", parts[0][1])
        self.assertIn("今天状态很好", parts[0][1])
        self.assertIn(("image_url", "https://i0.hdslb.com/comment-image.png"), parts)
        self.assertEqual(parts[-1][0], "plain")
        self.assertIn("https://www.bilibili.com/video/BV1xx411c7mD", parts[-1][1])

    def test_comment_notification_uses_card_and_commenter_profile(self) -> None:
        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime
        rendered = []

        class RecordingRenderer:
            async def render(self, notification):
                rendered.append(notification)
                return "/tmp/bilibili-comment-card.png"

        async def profile(uid, *, fallback=None):
            return BilibiliAuthorCardProfile(
                uid=uid,
                name=fallback.name,
                follower=1234,
            )

        runtime.card_renderer = RecordingRenderer()
        runtime.get_author_card_profile = profile
        notification = BilibiliNotification(
            kind="comment",
            uid="200",
            author_name="评论者",
            title="",
            text="评论正文",
            url="https://t.bilibili.com/1",
            content_id="9001",
            comment_created_at=1_700_000_000,
        )

        parts = asyncio.run(runtime.build_card_or_fallback_parts(notification))

        self.assertEqual(parts[0], ("image", "/tmp/bilibili-comment-card.png"))
        self.assertEqual(parts[1], ("plain", "https://t.bilibili.com/1"))
        self.assertEqual(rendered[0].author_profile.follower, 1234)
        self.assertEqual(rendered[0].published_at, 1_700_000_000)

    def test_card_result_is_image_plus_clickable_url_and_only_live_uses_atall(self) -> None:
        from asoul_bilibili_runtime import BilibiliPushTarget

        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime
        rendered_kinds = []

        class FakeRenderer:
            async def render(self, notification):
                rendered_kinds.append(notification.kind)
                return "/tmp/bilibili-card.png"

        async def allow_atall(_target):
            return True

        runtime.card_renderer = FakeRenderer()
        runtime.should_send_live_atall = allow_atall
        target = BilibiliPushTarget(
            group_id="100",
            platform_name="aiocqhttp",
            unified_msg_origin="aiocqhttp:GroupMessage:100",
        )
        dynamic = BilibiliNotification(
            kind="dynamic",
            uid="100",
            author_name="测试账号",
            title="",
            url="https://t.bilibili.com/1",
        )
        live = BilibiliNotification(
            kind="live",
            uid="100",
            author_name="测试账号",
            title="开播了",
            url="https://live.bilibili.com/1",
        )

        dynamic_result = asyncio.run(runtime.build_notification_result(dynamic, target))
        live_result = asyncio.run(runtime.build_notification_result(live, target))

        self.assertEqual(dynamic_result.chain[0], ("image", "/tmp/bilibili-card.png"))
        self.assertEqual(dynamic_result.chain[1], ("plain", "https://t.bilibili.com/1"))
        self.assertFalse(any(isinstance(part, self.main.Comp.AtAll) for part in dynamic_result.chain))
        self.assertIsInstance(live_result.chain[0], self.main.Comp.AtAll)
        self.assertEqual(live_result.chain[-2], ("image", "/tmp/bilibili-card.png"))
        self.assertEqual(live_result.chain[-1], ("plain", "https://live.bilibili.com/1"))
        self.assertEqual(rendered_kinds, ["dynamic", "live"])

    def test_card_render_failure_falls_back_to_legacy_notification(self) -> None:
        from asoul_bilibili_runtime import BilibiliPushTarget

        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime

        class FailingRenderer:
            async def render(self, _notification):
                raise RuntimeError("render failed")

        runtime.card_renderer = FailingRenderer()
        target = BilibiliPushTarget(
            group_id="100",
            platform_name="aiocqhttp",
            unified_msg_origin="aiocqhttp:GroupMessage:100",
        )
        notification = BilibiliNotification(
            kind="video",
            uid="100",
            author_name="测试账号",
            title="新视频",
            url="https://www.bilibili.com/video/BV1",
        )

        result = asyncio.run(runtime.build_notification_result(notification, target))

        self.assertIn("【B站新视频】", result.chain[0][1])
        self.assertIn("https://www.bilibili.com/video/BV1", result.chain[-1][1])

    def test_card_config_switch_bypasses_renderer(self) -> None:
        from asoul_bilibili_runtime import BilibiliPushTarget

        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime
        runtime.push_config = replace(
            runtime.push_config,
            render_bilibili_cards=False,
        )

        class UnexpectedRenderer:
            async def render(self, _notification):
                raise AssertionError("renderer should be bypassed")

        runtime.card_renderer = UnexpectedRenderer()
        target = BilibiliPushTarget(
            group_id="100",
            platform_name="aiocqhttp",
            unified_msg_origin="aiocqhttp:GroupMessage:100",
        )
        notification = BilibiliNotification(
            kind="dynamic",
            uid="100",
            author_name="测试账号",
            title="",
            text="旧格式正文",
            url="https://t.bilibili.com/1",
        )

        result = asyncio.run(runtime.build_notification_result(notification, target))

        self.assertIn("【B站动态】", result.chain[0][1])
        self.assertIn(("plain", "旧格式正文"), result.chain)

    def test_video_card_uses_authoritative_stats_and_runtime_cache(self) -> None:
        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime
        fetch_calls = []
        rendered_notifications = []

        async def fetch_stats(bvid):
            fetch_calls.append(bvid)
            return BilibiliEngagementStats(70696, 5468, 6081)

        class RecordingRenderer:
            async def render(self, notification):
                rendered_notifications.append(notification)
                return "/tmp/bilibili-card.png"

        runtime.gateway.get_video_engagement_stats = fetch_stats
        runtime.card_renderer = RecordingRenderer()
        notification = BilibiliNotification(
            kind="video",
            uid="100",
            author_name="测试账号",
            title="新视频",
            url="https://www.bilibili.com/video/BV1SdXWB2Enp",
            content_id="dyn-video",
            video_bvid="BV1SdXWB2Enp",
            stats=BilibiliEngagementStats(1, 2, 3),
        )

        async def exercise():
            await runtime.build_card_or_fallback_parts(notification)
            await runtime.build_card_or_fallback_parts(
                replace(
                    notification,
                    stats=BilibiliEngagementStats(999, 888, 777),
                )
            )

        asyncio.run(exercise())

        self.assertEqual(fetch_calls, ["BV1SdXWB2Enp"])
        self.assertEqual(len(rendered_notifications), 2)
        self.assertTrue(
            all(
                item.stats == BilibiliEngagementStats(70696, 5468, 6081)
                for item in rendered_notifications
            )
        )
        self.assertTrue(all(not item.stats_are_fallback for item in rendered_notifications))

    def test_video_detail_timeout_keeps_fallback_stats_and_marks_card(self) -> None:
        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime
        rendered_notifications = []

        async def hang(_bvid):
            await asyncio.Event().wait()

        class RecordingRenderer:
            async def render(self, notification):
                rendered_notifications.append(notification)
                return "/tmp/bilibili-card.png"

        runtime.gateway.get_video_engagement_stats = hang
        runtime.card_renderer = RecordingRenderer()
        fallback = BilibiliEngagementStats(10, 20, 30)
        notification = BilibiliNotification(
            kind="video",
            uid="100",
            author_name="测试账号",
            title="新视频",
            url="https://www.bilibili.com/video/BV1SdXWB2Enp",
            video_bvid="BV1SdXWB2Enp",
            stats=fallback,
        )

        with patch("asoul_bilibili_runtime.VIDEO_STATS_TIMEOUT_SECONDS", 0.001):
            asyncio.run(runtime.build_card_or_fallback_parts(notification))

        self.assertEqual(rendered_notifications[0].stats, fallback)
        self.assertTrue(rendered_notifications[0].stats_are_fallback)

    def test_disabled_cards_do_not_fetch_video_stats(self) -> None:
        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime
        runtime.push_config = replace(runtime.push_config, render_bilibili_cards=False)

        async def unexpected_fetch(_bvid):
            raise AssertionError("video stats should not be fetched")

        runtime.gateway.get_video_engagement_stats = unexpected_fetch
        notification = BilibiliNotification(
            kind="video",
            uid="100",
            author_name="测试账号",
            title="新视频",
            url="https://www.bilibili.com/video/BV1SdXWB2Enp",
            video_bvid="BV1SdXWB2Enp",
        )

        parts = asyncio.run(runtime.build_card_or_fallback_parts(notification))

        self.assertIn("【B站新视频】", parts[0][1])

    def test_video_without_bvid_marks_existing_stats_as_fallback(self) -> None:
        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime
        rendered_notifications = []

        class RecordingRenderer:
            async def render(self, notification):
                rendered_notifications.append(notification)
                return "/tmp/bilibili-card.png"

        runtime.card_renderer = RecordingRenderer()
        notification = BilibiliNotification(
            kind="video",
            uid="100",
            author_name="测试账号",
            title="旧视频",
            url="https://www.bilibili.com/video/av123",
            stats=BilibiliEngagementStats(10, 20, 30),
        )

        asyncio.run(runtime.build_card_or_fallback_parts(notification))

        self.assertEqual(rendered_notifications[0].stats, notification.stats)
        self.assertTrue(rendered_notifications[0].stats_are_fallback)

    def test_video_test_notification_carries_bvid_for_production_enrichment(self) -> None:
        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime

        async def recent_videos(_uid, stop_at_id=None):
            return [
                BilibiliVideoPost(
                    id="BV1SdXWB2Enp",
                    title="测试视频",
                    url="https://www.bilibili.com/video/BV1SdXWB2Enp",
                    cover_url="https://i0.hdslb.com/video.jpg",
                    created_at=1_700_000_000,
                )
            ]

        async def user_name(_uid):
            return "测试账号"

        async def profile(uid, fallback):
            return BilibiliAuthorCardProfile(uid=uid, name=fallback.name)

        runtime.gateway.get_recent_videos = recent_videos
        runtime.gateway.get_user_name = user_name
        runtime.get_author_card_profile = profile

        notification = asyncio.run(
            plugin._bilibili_commands.build_video_test_notification("100")
        )

        self.assertIsNotNone(notification)
        assert notification is not None
        self.assertEqual(notification.video_bvid, "BV1SdXWB2Enp")

    def test_empty_delivery_plan_does_not_fetch_video_stats(self) -> None:
        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime
        origin = "aiocqhttp:GroupMessage:100"
        runtime.push_targets = {
            origin: {
                "group_id": "100",
                "platform_name": "aiocqhttp",
                "unified_msg_origin": origin,
            }
        }

        class BaselineMonitor:
            async def fetch_uid_snapshot(self, config, uid, previous_state=None):
                from asoul_bilibili import BilibiliUidSnapshot

                return BilibiliUidSnapshot(uid=uid, author_name="测试账号")

            def plan_uid_deliveries(self, config, previous_state, snapshot):
                return BilibiliUidDeliveryPlan(
                    deliveries=[],
                    final_state={"author_name": snapshot.author_name},
                )

        async def unexpected_fetch(_bvid):
            raise AssertionError("baseline poll must not fetch video detail")

        async def profile(_uid, fallback):
            return fallback

        runtime.monitor = BaselineMonitor()
        runtime.gateway.get_video_engagement_stats = unexpected_fetch
        runtime.get_author_card_profile = profile

        asyncio.run(runtime.poll_bilibili_updates_for_uid("100"))

    def test_card_render_timeout_falls_back_to_legacy_notification(self) -> None:
        from unittest.mock import patch

        from asoul_bilibili_runtime import BilibiliPushTarget

        plugin = self._new_plugin(["100"])
        runtime = plugin._bilibili_runtime

        class HangingRenderer:
            async def render(self, _notification):
                await asyncio.Event().wait()

        runtime.card_renderer = HangingRenderer()
        target = BilibiliPushTarget(
            group_id="100",
            platform_name="aiocqhttp",
            unified_msg_origin="aiocqhttp:GroupMessage:100",
        )
        notification = BilibiliNotification(
            kind="video",
            uid="100",
            author_name="测试账号",
            title="新视频",
            url="https://www.bilibili.com/video/BV1",
        )

        with patch("asoul_bilibili_runtime.CARD_RENDER_TIMEOUT_SECONDS", 0.01):
            result = asyncio.run(runtime.build_notification_result(notification, target))

        self.assertIn("【B站新视频】", result.chain[0][1])


if __name__ == "__main__":
    unittest.main()
