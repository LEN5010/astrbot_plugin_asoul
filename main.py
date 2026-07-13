import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from asoul_bilibili_commands import BilibiliCommandService
from asoul_bilibili_runtime import BilibiliRuntime
from asoul_calendar import CalendarRepository
from asoul_core import (
    DISPLAY_TZ,
    HELP_MESSAGE,
    HELP_TRIGGER_TEXTS,
    LIVE_REQUEST_FILTERED_HOSTS,
    NO_NEXT_WEEK_SCHEDULE_TEXT,
    THIS_WEEK_TRIGGER_TEXTS,
    TODAY_TRIGGER_TEXTS,
    TOMORROW_TRIGGER_TEXTS,
    parse_live_request,
)
from asoul_render import ScheduleImageRenderer
from asoul_schedule import ScheduleService

DEFAULT_CALENDAR_CACHE_MINUTES = 30
MIN_CALENDAR_CACHE_MINUTES = 10


def _build_calendar_cache_ttl(config: Any) -> timedelta:
    raw_minutes: Any = DEFAULT_CALENDAR_CACHE_MINUTES
    getter = getattr(config, "get", None)
    if callable(getter):
        raw_minutes = getter("calendar_cache_minutes", DEFAULT_CALENDAR_CACHE_MINUTES)

    try:
        minutes = int(raw_minutes)
    except (TypeError, ValueError):
        minutes = DEFAULT_CALENDAR_CACHE_MINUTES

    return timedelta(minutes=max(MIN_CALENDAR_CACHE_MINUTES, minutes))


@register("astrbot_plugin_asoul", "LEN5010", "查询 A-SOUL 今日直播安排", "v3.0.2")
class ASoulPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._calendar_repository = CalendarRepository(
            cache_ttl=_build_calendar_cache_ttl(self.config)
        )
        self._schedule_service = ScheduleService()
        self._image_renderer = ScheduleImageRenderer()
        self._bilibili_runtime = BilibiliRuntime(self, context, self.config)
        self._bilibili_commands = BilibiliCommandService(self._bilibili_runtime)

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        await self._bilibili_runtime.ensure_ready()

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def remember_group_origin(self, event: AstrMessageEvent):
        await self._bilibili_runtime.remember_group_origin(event)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_bot_help(self, event: AstrMessageEvent):
        """用户发送 /bot帮助 时返回使用说明。"""
        if event.message_str.strip() not in HELP_TRIGGER_TEXTS:
            return

        event.stop_event()
        yield event.plain_result(HELP_MESSAGE)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_live_request(self, event: AstrMessageEvent):
        """用户发送“今日直播”“明日直播”或“本周直播”时返回直播安排。"""
        parsed_request = parse_live_request(event.message_str)
        if parsed_request is None:
            return
        message_text, exclude_filtered_hosts = parsed_request

        event.stop_event()
        today = datetime.now(DISPLAY_TZ).date()
        if message_text in THIS_WEEK_TRIGGER_TEXTS:
            week_end = today + timedelta(days=6 - today.weekday())
            try:
                day_events = await self._calendar_repository.get_live_events_for_days(today, week_end)
            except Exception:
                logger.exception("获取 A-SOUL 本周直播日历失败")
                yield event.plain_result("⚠️ 直播日历暂时不可用，请稍后再试。")
                return

            day_items = []
            for target_day in sorted(day_events):
                items = self._schedule_service.build_schedule_items(day_events.get(target_day, []))
                if exclude_filtered_hosts:
                    items = self._schedule_service.exclude_hosts(items, LIVE_REQUEST_FILTERED_HOSTS)
                day_items.append((target_day, items))
            try:
                image_url = await self._image_renderer.render_week_schedule_image(
                    day_items,
                    f"{today.strftime('%Y-%m-%d')} 起 本周直播",
                )
            except Exception:
                logger.exception("渲染本周直播图片失败")
                fallback_lines = []
                for target_day, items in day_items:
                    fallback_lines.append(
                        self._schedule_service.format_schedule_fallback(
                            items,
                            target_day,
                            "本周直播",
                        )
                    )
                yield event.plain_result("\n\n".join(fallback_lines))
                return

            yield event.image_result(image_url)
            return

        if message_text in TOMORROW_TRIGGER_TEXTS:
            if today.weekday() == 6:
                yield event.plain_result(NO_NEXT_WEEK_SCHEDULE_TEXT)
                return
            target_day = today + timedelta(days=1)
            title_text = "明日直播"
        else:
            target_day = today
            title_text = "今日直播"

        try:
            events = await self._calendar_repository.get_live_events_for_day(target_day)
        except Exception:
            logger.exception("获取 A-SOUL 直播日历失败")
            yield event.plain_result("⚠️ 直播日历暂时不可用，请稍后再试。")
            return

        items = self._schedule_service.build_schedule_items(events)
        if exclude_filtered_hosts:
            items = self._schedule_service.exclude_hosts(items, LIVE_REQUEST_FILTERED_HOSTS)
        try:
            image_url = await self._image_renderer.render_schedule_image(items, target_day, title_text)
        except Exception:
            logger.exception("渲染直播图片失败")
            yield event.plain_result(self._schedule_service.format_schedule_fallback(items, target_day, title_text))
            return

        yield event.image_result(image_url)

    async def terminate(self):
        """插件卸载时调用。"""
        await self._bilibili_runtime.terminate()
        return None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_dynamic")
    async def bili_test_dynamic(self, event: AstrMessageEvent, uid: str):
        error_text = await self._bilibili_commands.ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._bilibili_commands.normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        notification = await self._bilibili_commands.build_dynamic_test_notification(uid)
        if notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用动态。")
            return

        yield event.chain_result(self._bilibili_runtime.build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_dump_dynamic")
    async def bili_dump_dynamic(self, event: AstrMessageEvent, uid: str):
        error_text = await self._bilibili_commands.ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._bilibili_commands.normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        file_path = await self._bilibili_commands.dump_dynamic_payload(uid)
        yield event.plain_result(f"已导出动态原始 payload: {file_path}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_video")
    async def bili_test_video(self, event: AstrMessageEvent, uid: str):
        error_text = await self._bilibili_commands.ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._bilibili_commands.normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        notification = await self._bilibili_commands.build_video_test_notification(uid)
        if notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用视频。")
            return

        yield event.chain_result(self._bilibili_runtime.build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_live")
    async def bili_test_live(self, event: AstrMessageEvent, uid: str):
        error_text = await self._bilibili_commands.ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._bilibili_commands.normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        plain_text, notification = await self._bilibili_commands.build_live_test_notification(uid)
        if notification is None:
            yield event.plain_result(plain_text)
            return

        yield event.chain_result(self._bilibili_runtime.build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_dump_live")
    async def bili_dump_live(self, event: AstrMessageEvent, uid: str):
        error_text = await self._bilibili_commands.ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._bilibili_commands.normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        file_path = await self._bilibili_commands.dump_live_payload(uid)
        yield event.plain_result(f"已导出直播原始 payload: {file_path}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_atall")
    async def bili_test_atall(self, event: AstrMessageEvent):
        error_text = await self._bilibili_runtime.send_atall_test(event)
        if error_text:
            yield event.plain_result(error_text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_all")
    async def bili_test_all(self, event: AstrMessageEvent, uid: str):
        error_text = await self._bilibili_commands.ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._bilibili_commands.normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        yield event.plain_result(f"开始测试抓取 UID {uid}")

        dynamic_notification = await self._bilibili_commands.build_dynamic_test_notification(uid)
        if dynamic_notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用动态。")
        else:
            yield event.chain_result(self._bilibili_runtime.build_notification_parts(dynamic_notification))

        video_notification = await self._bilibili_commands.build_video_test_notification(uid)
        if video_notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用视频。")
        else:
            yield event.chain_result(self._bilibili_runtime.build_notification_parts(video_notification))

        live_text, live_notification = await self._bilibili_commands.build_live_test_notification(uid)
        if live_notification is None:
            yield event.plain_result(live_text)
        else:
            yield event.chain_result(self._bilibili_runtime.build_notification_parts(live_notification))

        comment_notifications = await self._bilibili_commands.build_comment_test_notifications(uid)
        if not comment_notifications:
            yield event.plain_result(f"UID {uid} 当前最近资源下没有抓到目标评论。")
        else:
            for notification in comment_notifications:
                yield event.chain_result(self._bilibili_runtime.build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_comment")
    async def bili_test_comment(self, event: AstrMessageEvent, uid: str):
        error_text = await self._bilibili_commands.ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._bilibili_commands.normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        notifications = await self._bilibili_commands.build_comment_test_notifications(uid)
        if not notifications:
            yield event.plain_result(f"UID {uid} 当前最近资源下没有抓到目标评论。")
            return
        for notification in notifications:
            yield event.chain_result(self._bilibili_runtime.build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_status")
    async def bili_status(self, event: AstrMessageEvent):
        yield event.plain_result(
            await self._bilibili_runtime.build_bilibili_status_text()
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_login")
    async def bili_login(self, event: AstrMessageEvent):
        if event.message_obj.group_id:
            yield event.plain_result("请在私聊中使用 /bili_login。")
            return

        qr_login, qr_code_path = await self._bilibili_commands.create_login_qrcode()

        yield event.chain_result(
            [
                Comp.Plain("请使用哔哩哔哩 App 扫描二维码登录。"),
                Comp.Image.fromFileSystem(str(qr_code_path)),
            ]
        )

        try:
            yield event.plain_result(
                await self._bilibili_commands.wait_for_login(qr_login)
            )
        except Exception:
            logger.exception("B 站二维码登录失败")
            yield event.plain_result("B 站登录失败，请查看日志。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_logout")
    async def bili_logout(self, event: AstrMessageEvent):
        await self._bilibili_commands.logout()
        yield event.plain_result("已清除当前保存的 B 站登录态。")
