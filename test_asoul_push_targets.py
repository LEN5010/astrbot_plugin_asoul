import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


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
        return self.main.ASoulPlugin(
            self.context,
            config={
                "enabled": False,
                "group_whitelist": list(group_whitelist),
                "target_uids": ["672328094"],
            },
        )

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

        targets = plugin._get_active_push_targets()

        self.assertEqual(
            sorted(target.group_id for target in targets),
            ["100", "200"],
        )
        self.assertEqual(
            sorted(target.unified_msg_origin for target in targets),
            ["aiocqhttp:GroupMessage:100", "aiocqhttp:GroupMessage:200"],
        )

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

        targets = plugin._get_active_push_targets()

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].group_id, "100")
        self.assertEqual(targets[0].unified_msg_origin, "aiocqhttp:GroupMessage:100_new")
        self.assertNotIn("aiocqhttp:GroupMessage:100_old", plugin._bilibili_push_targets)

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

        targets = plugin._get_active_push_targets()

        self.assertEqual(
            sorted(target.group_id for target in targets),
            ["100", "200"],
        )

    def test_load_runtime_state_normalizes_legacy_group_origin_mapping(self) -> None:
        plugin = self._new_plugin(["100", "200"])
        plugin._kv_store[self.main.KV_BILIBILI_GROUP_ORIGINS] = {
            "100": "aiocqhttp:GroupMessage:100",
            "200": "aiocqhttp:GroupMessage:200",
        }

        asyncio.run(plugin._load_bilibili_runtime_state())

        targets = plugin._get_active_push_targets()

        self.assertEqual(
            sorted(target.group_id for target in targets),
            ["100", "200"],
        )
        self.assertIn("aiocqhttp:GroupMessage:100", plugin._bilibili_push_targets)
        self.assertIn("aiocqhttp:GroupMessage:200", plugin._bilibili_push_targets)

    def test_comment_notification_parts_render_new_format_with_images(self) -> None:
        plugin = self._new_plugin(["100"])
        notification = self.main.BilibiliNotification(
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

        parts = plugin._build_notification_parts(notification)

        self.assertEqual(parts[0][0], "plain")
        self.assertIn("【B站评论】乃琳Queen", parts[0][1])
        self.assertIn("乃琳Queen于2023-11-15 06:13", parts[0][1])
        self.assertIn("在嘉然今天吃什么的视频《鸣潮3.1主线上半！》下发表了评论：", parts[0][1])
        self.assertIn("今天状态很好", parts[0][1])
        self.assertIn(("image_url", "https://i0.hdslb.com/comment-image.png"), parts)
        self.assertEqual(parts[-1][0], "plain")
        self.assertIn("https://www.bilibili.com/video/BV1xx411c7mD", parts[-1][1])


if __name__ == "__main__":
    unittest.main()
