import asyncio
from datetime import datetime, timedelta

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

from asoul_bilibili import (
    KV_BILIBILI_GROUP_ORIGINS,
    KV_BILIBILI_MONITOR_STATE,
    BilibiliGateway,
    BilibiliMonitorService,
    build_bilibili_push_config,
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


@register("astrbot_plugin_asoul", "LEN5010", "查询 A-SOUL 今日直播安排", "1.1.0")
class ASoulPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._calendar_repository = CalendarRepository()
        self._schedule_service = ScheduleService()
        self._image_renderer = ScheduleImageRenderer()
        self._bilibili_config = build_bilibili_push_config(self.config)
        self._bilibili_monitor = BilibiliMonitorService(
            BilibiliGateway(request_client=self._bilibili_config.request_client)
        )
        self._bilibili_task: asyncio.Task | None = None
        self._bilibili_group_origins: dict[str, str] = {}
        self._bilibili_monitor_state: dict = {}

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
        self._bilibili_group_origins = group_origins if isinstance(group_origins, dict) else {}
        self._bilibili_monitor_state = monitor_state if isinstance(monitor_state, dict) else {}

    async def _run_bilibili_monitor_loop(self) -> None:
        logger.info("启动 B 站自动播报任务，轮询间隔 %s 秒", self._bilibili_config.poll_interval_seconds)
        try:
            while True:
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
                    chain = self._build_notification_chain(notification)
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

    def _build_notification_chain(self, notification) -> MessageChain:
        if notification.kind == "live":
            return MessageChain(
                [
                    Comp.At(qq="all"),
                    Comp.Plain("\n" + notification.render_text()),
                ]
            )
        return MessageChain([Comp.Plain(notification.render_text())])
