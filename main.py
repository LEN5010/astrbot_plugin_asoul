from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

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
    def __init__(self, context: Context):
        super().__init__(context)
        self._calendar_repository = CalendarRepository()
        self._schedule_service = ScheduleService()
        self._image_renderer = ScheduleImageRenderer()

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
        return None
