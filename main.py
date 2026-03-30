import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Tuple

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from bilibili_api import login_v2

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from asoul_bilibili import (
    KV_BILIBILI_CREDENTIAL,
    KV_BILIBILI_GROUP_ORIGINS,
    KV_BILIBILI_MONITOR_STATE,
    BilibiliGateway,
    BilibiliMonitorService,
    BilibiliNotification,
    BilibiliRichTextNode,
    build_bilibili_push_config,
    normalize_bilibili_credential_data,
)
from asoul_calendar import CalendarRepository
from asoul_core import (
    DISPLAY_TZ,
    HELP_MESSAGE,
    HELP_TRIGGER_TEXTS,
    NO_NEXT_WEEK_SCHEDULE_TEXT,
    TODAY_TRIGGER_TEXTS,
    TOMORROW_TRIGGER_TEXTS,
)
from asoul_render import ScheduleImageRenderer
from asoul_schedule import ScheduleService

GROUP_MESSAGE_TYPE = "GroupMessage"
MIN_AT_ALL_REMAINING = 1
QR_CODE_PATH = Path(__file__).resolve().parent / "temp" / "bilibili_login_qrcode.png"


@register("astrbot_plugin_asoul", "LEN5010", "查询 A-SOUL 今日直播安排", "1.1.0")
class ASoulPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._calendar_repository = CalendarRepository()
        self._schedule_service = ScheduleService()
        self._image_renderer = ScheduleImageRenderer()
        self._bilibili_config = build_bilibili_push_config(self.config)
        self._bilibili_gateway = BilibiliGateway(
            request_client=self._bilibili_config.request_client,
            credential_data=self._bilibili_config.credential_data,
        )
        self._bilibili_monitor = BilibiliMonitorService(
            self._bilibili_gateway
        )
        self._bilibili_task: asyncio.Task | None = None
        self._bilibili_group_origins: dict[str, str] = {}
        self._bilibili_monitor_state: dict = {}
        self._bilibili_credential_data: dict[str, str] = {}
        self._bilibili_missing_login_logged = False

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        await self._load_bilibili_runtime_state()
        if not self._bilibili_config.enabled:
            logger.info("B 站自动播报未启用")
            return
        if not self._bilibili_config.target_uids:
            logger.info("B 站自动播报未配置目标 UID")
            return

        if self._bilibili_task and not self._bilibili_task.done():
            return

        self._bilibili_task = asyncio.create_task(self._run_bilibili_monitor_loop())

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def remember_group_origin(self, event: AstrMessageEvent):
        group_id = str(getattr(event.message_obj, "group_id", "") or "").strip()
        if not group_id:
            return
        if group_id not in self._bilibili_config.group_whitelist:
            return

        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not unified_msg_origin:
            return
        if self._bilibili_group_origins.get(group_id) == unified_msg_origin:
            return

        self._bilibili_group_origins[group_id] = unified_msg_origin
        await self.put_kv_data(KV_BILIBILI_GROUP_ORIGINS, self._bilibili_group_origins)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_bot_help(self, event: AstrMessageEvent):
        """用户发送 /bot帮助 时返回使用说明。"""
        if event.message_str.strip() not in HELP_TRIGGER_TEXTS:
            return

        event.stop_event()
        yield event.plain_result(HELP_MESSAGE)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_live_request(self, event: AstrMessageEvent):
        """用户发送“今日直播”或“明日直播”时返回直播安排。"""
        message_text = event.message_str.strip()
        if message_text not in TODAY_TRIGGER_TEXTS and message_text not in TOMORROW_TRIGGER_TEXTS:
            return

        event.stop_event()
        today = datetime.now(DISPLAY_TZ).date()
        if message_text in TODAY_TRIGGER_TEXTS:
            target_day = today
            title_text = "今日直播"
        else:
            if today.weekday() == 6:
                yield event.plain_result(NO_NEXT_WEEK_SCHEDULE_TEXT)
                return
            target_day = today + timedelta(days=1)
            title_text = "明日直播"

        try:
            events = await self._calendar_repository.get_live_events_for_day(target_day)
        except Exception:
            logger.exception("获取 A-SOUL 直播日历失败")
            yield event.plain_result("⚠️ 直播日历暂时不可用，请稍后再试。")
            return

        items = self._schedule_service.build_schedule_items(events)
        try:
            image_url = await self._image_renderer.render_schedule_image(items, target_day, title_text)
        except Exception:
            logger.exception("渲染直播图片失败")
            yield event.plain_result(self._schedule_service.format_schedule_fallback(items, target_day, title_text))
            return

        yield event.image_result(image_url)

    async def terminate(self):
        """插件卸载时调用。"""
        if self._bilibili_task and not self._bilibili_task.done():
            self._bilibili_task.cancel()
            try:
                await self._bilibili_task
            except asyncio.CancelledError:
                pass
        return None

    async def _load_bilibili_runtime_state(self) -> None:
        group_origins = await self.get_kv_data(KV_BILIBILI_GROUP_ORIGINS, {})
        monitor_state = await self.get_kv_data(KV_BILIBILI_MONITOR_STATE, {})
        credential_data = await self.get_kv_data(KV_BILIBILI_CREDENTIAL, {})
        self._bilibili_group_origins = group_origins if isinstance(group_origins, dict) else {}
        self._bilibili_monitor_state = monitor_state if isinstance(monitor_state, dict) else {}
        self._bilibili_credential_data = self._resolve_bilibili_credential_data(credential_data)
        self._bilibili_gateway.set_credential_data(self._bilibili_credential_data)

    async def _run_bilibili_monitor_loop(self) -> None:
        logger.info("启动 B 站自动播报任务，轮询间隔 %s 秒", self._bilibili_config.poll_interval_seconds)
        try:
            while True:
                if not self._bilibili_gateway.has_credential():
                    if not self._bilibili_missing_login_logged:
                        logger.warning("B 站自动播报未登录，轮询已暂停。请配置凭据或使用 /bili_login 登录。")
                        self._bilibili_missing_login_logged = True
                    await asyncio.sleep(self._bilibili_config.poll_interval_seconds)
                    continue

                self._bilibili_missing_login_logged = False
                await self._poll_bilibili_updates_once()
                await asyncio.sleep(self._bilibili_config.poll_interval_seconds)
        except asyncio.CancelledError:
            logger.info("B 站自动播报任务已停止")
            raise

    async def _poll_bilibili_updates_once(self) -> None:
        updated_state, notifications = await self._bilibili_monitor.poll(
            config=self._bilibili_config,
            state=self._bilibili_monitor_state,
        )
        self._bilibili_monitor_state = updated_state
        await self.put_kv_data(KV_BILIBILI_MONITOR_STATE, self._bilibili_monitor_state)

        if not notifications:
            return

        target_origins = self._get_active_push_origins()
        if not target_origins:
            logger.info("存在 B 站新通知，但当前没有已登记的白名单群")
            return

        for notification in notifications:
            for unified_msg_origin in target_origins:
                try:
                    chain = await self._build_notification_chain(notification, unified_msg_origin)
                    await self.context.send_message(unified_msg_origin, chain)
                except Exception:
                    logger.exception("发送 B 站播报失败: uid=%s kind=%s", notification.uid, notification.kind)

    def _get_active_push_origins(self) -> list[str]:
        origins: list[str] = []
        seen = set()
        for group_id in self._bilibili_config.group_whitelist:
            unified_msg_origin = self._bilibili_group_origins.get(group_id)
            if not unified_msg_origin or unified_msg_origin in seen:
                continue
            seen.add(unified_msg_origin)
            origins.append(unified_msg_origin)
        return origins

    async def _build_notification_chain(self, notification, unified_msg_origin: str) -> MessageChain:
        chain_parts = self._build_notification_parts(notification)
        if notification.kind == "live" and await self._should_send_live_atall(unified_msg_origin):
            chain_parts = [Comp.AtAll(), Comp.Plain(" ")] + chain_parts
        return MessageChain(chain_parts)

    def _build_notification_parts(self, notification) -> list[Any]:
        prefix_map = {
            "dynamic": "【B站动态】",
            "video": "【B站新视频】",
            "live": "【B站开播】",
        }
        prefix = prefix_map.get(notification.kind, "【B站通知】")
        chain_parts: list[Any] = [Comp.Plain(f"{prefix}{notification.author_name}\n")]

        if notification.kind == "dynamic":
            self._append_rich_text_parts(chain_parts, notification.rich_nodes, notification.text)
            for image_url in notification.image_urls:
                chain_parts.append(Comp.Plain("\n"))
                chain_parts.append(Comp.Image.fromURL(image_url))
        else:
            title = str(notification.title or "").strip()
            if title:
                chain_parts.append(Comp.Plain(title))
            cover_url = str(notification.cover_url or "").strip()
            if cover_url:
                chain_parts.append(Comp.Plain("\n"))
                chain_parts.append(Comp.Image.fromURL(cover_url))

        chain_parts.append(Comp.Plain(f"\n{notification.url}"))
        return chain_parts

    def _append_rich_text_parts(
        self,
        chain_parts: list[Any],
        rich_nodes: list[BilibiliRichTextNode],
        fallback_text: str,
    ) -> None:
        nodes = rich_nodes or []
        if not nodes:
            chain_parts.append(Comp.Plain(fallback_text or "发布了新动态"))
            return

        for node in nodes:
            if node.kind == "emoji" and node.image_url:
                chain_parts.append(Comp.Image.fromURL(node.image_url))
                continue
            if node.text:
                chain_parts.append(Comp.Plain(node.text))

    def _resolve_bilibili_credential_data(self, runtime_credential_data: Any) -> dict[str, str]:
        runtime_data = normalize_bilibili_credential_data(runtime_credential_data)
        if runtime_data:
            return runtime_data
        return normalize_bilibili_credential_data(self._bilibili_config.credential_data)

    async def _save_bilibili_credential(self, credential_data: dict[str, str]) -> None:
        normalized = normalize_bilibili_credential_data(credential_data)
        self._bilibili_credential_data = normalized
        self._bilibili_gateway.set_credential_data(normalized)
        await self.put_kv_data(KV_BILIBILI_CREDENTIAL, normalized)

    async def _clear_bilibili_credential(self) -> None:
        self._bilibili_credential_data = {}
        self._bilibili_gateway.clear_credential()
        await self.delete_kv_data(KV_BILIBILI_CREDENTIAL)

    def _ensure_private_bili_command(self, event: AstrMessageEvent) -> Optional[str]:
        if event.message_obj.group_id:
            return "请在私聊中使用这个指令。"
        if not self._bilibili_gateway.has_credential():
            return "当前未登录 B 站，请先使用 /bili_login。"
        return None

    async def _build_dynamic_test_notification(self, uid: str) -> Optional[BilibiliNotification]:
        posts = await self._bilibili_gateway.get_recent_dynamics(uid, stop_at_id=None)
        if not posts:
            return None

        post = posts[0]
        return BilibiliNotification(
            kind="dynamic",
            uid=uid,
            author_name=await self._bilibili_gateway.get_user_name(uid),
            title="",
            url=post.url,
            text=post.text,
            rich_nodes=post.rich_nodes,
            image_urls=post.image_urls,
        )

    async def _build_video_test_notification(self, uid: str) -> Optional[BilibiliNotification]:
        posts = await self._bilibili_gateway.get_recent_videos(uid, stop_at_id=None)
        if not posts:
            return None

        post = posts[0]
        return BilibiliNotification(
            kind="video",
            uid=uid,
            author_name=await self._bilibili_gateway.get_user_name(uid),
            title=post.title,
            url=post.url,
            cover_url=post.cover_url,
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_dynamic")
    async def bili_test_dynamic(self, event: AstrMessageEvent, uid: str):
        error_text = self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return

        notification = await self._build_dynamic_test_notification(uid)
        if notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用动态。")
            return

        yield event.chain_result(self._build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_video")
    async def bili_test_video(self, event: AstrMessageEvent, uid: str):
        error_text = self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return

        notification = await self._build_video_test_notification(uid)
        if notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用视频。")
            return

        yield event.chain_result(self._build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_live")
    async def bili_test_live(self, event: AstrMessageEvent, uid: str):
        error_text = self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return

        live_status = await self._bilibili_gateway.get_live_status(uid)
        if live_status is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到直播间信息。")
            return

        author_name = await self._bilibili_gateway.get_user_name(uid)
        if not live_status.is_live:
            yield event.plain_result(
                f"【B站直播状态】{author_name}\n当前未开播\n{live_status.url}"
            )
            return

        notification = BilibiliNotification(
            kind="live",
            uid=uid,
            author_name=author_name,
            title=live_status.title,
            url=live_status.url,
            cover_url=live_status.cover_url,
        )
        yield event.chain_result(self._build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_all")
    async def bili_test_all(self, event: AstrMessageEvent, uid: str):
        error_text = self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return

        yield event.plain_result(f"开始测试抓取 UID {uid}")

        dynamic_notification = await self._build_dynamic_test_notification(uid)
        if dynamic_notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用动态。")
        else:
            yield event.chain_result(self._build_notification_parts(dynamic_notification))

        video_notification = await self._build_video_test_notification(uid)
        if video_notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用视频。")
        else:
            yield event.chain_result(self._build_notification_parts(video_notification))

        live_status = await self._bilibili_gateway.get_live_status(uid)
        if live_status is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到直播间信息。")
            return

        author_name = await self._bilibili_gateway.get_user_name(uid)
        if not live_status.is_live:
            yield event.plain_result(
                f"【B站直播状态】{author_name}\n当前未开播\n{live_status.url}"
            )
            return

        yield event.chain_result(
            self._build_notification_parts(
                BilibiliNotification(
                    kind="live",
                    uid=uid,
                    author_name=author_name,
                    title=live_status.title,
                    url=live_status.url,
                    cover_url=live_status.cover_url,
                )
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_login")
    async def bili_login(self, event: AstrMessageEvent):
        if event.message_obj.group_id:
            yield event.plain_result("请在私聊中使用 /bili_login。")
            return

        QR_CODE_PATH.parent.mkdir(parents=True, exist_ok=True)
        qr_login = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
        await qr_login.generate_qrcode()
        qr_login.get_qrcode_picture().to_file(str(QR_CODE_PATH))

        yield event.chain_result(
            [
                Comp.Plain("请使用哔哩哔哩 App 扫描二维码登录。"),
                Comp.Image.fromFileSystem(str(QR_CODE_PATH)),
            ]
        )

        try:
            while True:
                state = await qr_login.check_state()
                if state == login_v2.QrCodeLoginEvents.DONE:
                    credential = qr_login.get_credential()
                    await self._save_bilibili_credential(credential.get_cookies())
                    yield event.plain_result("B 站登录成功，自动播报已恢复。")
                    return
                if state == login_v2.QrCodeLoginEvents.TIMEOUT:
                    yield event.plain_result("二维码已过期，请重新执行 /bili_login。")
                    return
                await asyncio.sleep(2)
        except Exception:
            logger.exception("B 站二维码登录失败")
            yield event.plain_result("B 站登录失败，请查看日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_logout")
    async def bili_logout(self, event: AstrMessageEvent):
        await self._clear_bilibili_credential()
        yield event.plain_result("已清除当前保存的 B 站登录态。")

    async def _should_send_live_atall(self, unified_msg_origin: str) -> bool:
        group_ctx = self._extract_group_session(unified_msg_origin)
        if not group_ctx:
            logger.info("直播 @全体仅支持群聊会话: %s", unified_msg_origin)
            return False

        platform_id, group_id = group_ctx
        platform_inst = self.context.get_platform_inst(platform_id)
        if not platform_inst:
            logger.warning("直播 @全体失败：找不到平台实例 %s", platform_id)
            return False

        client = platform_inst.get_client()
        if not client or not hasattr(client, "call_action"):
            logger.warning("直播 @全体失败：平台 %s 不支持 call_action", platform_id)
            return False

        try:
            group_id_param: int | str = int(group_id) if group_id.isdigit() else group_id
            remain_raw = await client.call_action(
                "get_group_at_all_remain",
                group_id=group_id_param,
            )
        except Exception:
            logger.exception("查询群 %s @全体剩余次数失败", group_id)
            return False

        remain_data = self._extract_action_data(remain_raw)
        can_at_all = bool(remain_data.get("can_at_all"))
        group_remain = int(remain_data.get("remain_at_all_count_for_group", 0) or 0)
        self_remain_value = remain_data.get(
            "remain_at_all_count_for_self",
            remain_data.get("remain_at_all_count_for_uin", 0),
        )
        self_remain = int(self_remain_value or 0)

        if not can_at_all:
            logger.info("群 %s 当前不允许 @全体成员", group_id)
            return False
        if group_remain < MIN_AT_ALL_REMAINING or self_remain < MIN_AT_ALL_REMAINING:
            logger.info(
                "群 %s @全体次数不足: group=%s, self=%s",
                group_id,
                group_remain,
                self_remain,
            )
            return False
        return True

    @staticmethod
    def _extract_group_session(unified_msg_origin: str) -> Optional[Tuple[str, str]]:
        try:
            platform_id, message_type, session_id = unified_msg_origin.split(":", 2)
        except ValueError:
            return None
        if message_type != GROUP_MESSAGE_TYPE:
            return None
        group_id = session_id.split("_")[-1].strip()
        if not group_id:
            return None
        return platform_id, group_id

    @staticmethod
    def _extract_action_data(action_result: Any) -> dict[str, Any]:
        if not isinstance(action_result, dict):
            return {}
        payload = action_result.get("data")
        if isinstance(payload, dict):
            return payload
        return action_result
