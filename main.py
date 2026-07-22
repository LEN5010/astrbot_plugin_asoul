import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

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
from asoul_schedule_highlight import ScheduleHighlightManager

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


def _build_comment_db_path() -> Path:
    data_dir = Path(
        StarTools.get_data_dir(plugin_name="astrbot_plugin_asoul")
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "bilibili_comments.sqlite3"


@register("astrbot_plugin_asoul", "LEN5010", "查询 A-SOUL 今日直播安排", "v3.6.0")
class ASoulPlugin(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.config = config or {}
        self._calendar_repository = CalendarRepository(
            cache_ttl=_build_calendar_cache_ttl(self.config)
        )
        self._schedule_service = ScheduleService()
        self._schedule_highlights = ScheduleHighlightManager(self)
        self._image_renderer = ScheduleImageRenderer()
        self._bilibili_runtime = BilibiliRuntime(
            self,
            context,
            self.config,
            comment_db_path=_build_comment_db_path(),
        )
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
                items = await self._schedule_highlights.apply(items)
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
        items = await self._schedule_highlights.apply(items)
        try:
            image_url = await self._image_renderer.render_schedule_image(items, target_day, title_text)
        except Exception:
            logger.exception("渲染直播图片失败")
            yield event.plain_result(self._schedule_service.format_schedule_fallback(items, target_day, title_text))
            return

        yield event.image_result(image_url)

    async def _get_schedule_items_for_admin(self, target_date_text: str):
        try:
            target_day = datetime.strptime(target_date_text, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("日期格式错误，请使用 YYYY-MM-DD。") from exc
        events = await self._calendar_repository.get_live_events_for_day(target_day)
        items = self._schedule_service.build_schedule_items(events)
        return target_day, await self._schedule_highlights.apply(items)

    @staticmethod
    def _format_schedule_candidates(target_day, items) -> str:
        if not items:
            return f"{target_day.isoformat()} 没有可标记的直播日程。"
        lines = [f"{target_day.isoformat()} 可标记日程："]
        for index, item in enumerate(items, start=1):
            marker = " ⭐特别关注" if item.highlighted else ""
            lines.append(
                f"{index}. {item.start_text}｜{item.hosts_text}｜{item.content}{marker}"
            )
        lines.append(
            f"使用 /日程高亮 {target_day.isoformat()} 序号 进行标记。"
        )
        return "\n".join(lines)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日程高亮")
    async def highlight_schedule(
        self,
        event: AstrMessageEvent,
        target_date: str = "",
        item_index: int = 0,
    ):
        """查看某天日程，或将指定序号标记为特别关注。"""
        if not target_date:
            yield event.plain_result(
                "用法：/日程高亮 YYYY-MM-DD [序号]"
            )
            return
        try:
            target_day, items = await self._get_schedule_items_for_admin(
                target_date
            )
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception:
            logger.exception("读取待高亮日程失败")
            yield event.plain_result("⚠️ 直播日历暂时不可用，请稍后再试。")
            return
        if item_index <= 0:
            yield event.plain_result(
                self._format_schedule_candidates(target_day, items)
            )
            return
        if item_index > len(items):
            yield event.plain_result(
                f"序号超出范围，当天共有 {len(items)} 条日程。"
            )
            return
        item = items[item_index - 1]
        await self._schedule_highlights.mark(item)
        yield event.plain_result(
            f"已设为特别关注：{target_day.isoformat()} "
            f"{item.start_text} {item.hosts_text}《{item.content}》"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消日程高亮")
    async def unhighlight_schedule(
        self,
        event: AstrMessageEvent,
        target_date: str = "",
        item_index: int = 0,
    ):
        """取消某天指定序号日程的特别关注。"""
        if not target_date or item_index <= 0:
            yield event.plain_result(
                "用法：/取消日程高亮 YYYY-MM-DD 序号"
            )
            return
        try:
            target_day, items = await self._get_schedule_items_for_admin(
                target_date
            )
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception:
            logger.exception("读取待取消高亮日程失败")
            yield event.plain_result("⚠️ 直播日历暂时不可用，请稍后再试。")
            return
        if item_index > len(items):
            yield event.plain_result(
                f"序号超出范围，当天共有 {len(items)} 条日程。"
            )
            return
        item = items[item_index - 1]
        removed = await self._schedule_highlights.unmark(item)
        if not removed:
            yield event.plain_result("该日程当前没有设置特别关注。")
            return
        yield event.plain_result(
            f"已取消特别关注：{target_day.isoformat()} "
            f"{item.start_text} {item.hosts_text}《{item.content}》"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("日程高亮列表")
    async def list_schedule_highlights(self, event: AstrMessageEvent):
        """列出当前保存的全部特别关注日程。"""
        records = await self._schedule_highlights.list_records()
        if not records:
            yield event.plain_result("当前没有特别关注日程。")
            return
        lines = ["【特别关注日程】"]
        for index, record in enumerate(records, start=1):
            lines.append(
                f"{index}. ⭐ {record.target_date} {record.start_text}｜"
                f"{record.hosts_text}｜{record.content}"
            )
        lines.append("日历中已消失的节目可使用 /取消日程高亮记录 序号 移除。")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("取消日程高亮记录")
    async def remove_schedule_highlight_record(
        self,
        event: AstrMessageEvent,
        record_index: int = 0,
    ):
        """按保存列表序号移除特别关注记录。"""
        records = await self._schedule_highlights.list_records()
        if record_index <= 0 or record_index > len(records):
            yield event.plain_result(
                f"用法：/取消日程高亮记录 序号（当前共 {len(records)} 条）"
            )
            return
        record = records[record_index - 1]
        await self._schedule_highlights.unmark_key(record.key)
        yield event.plain_result(
            f"已取消特别关注：{record.target_date} {record.start_text} "
            f"{record.hosts_text}《{record.content}》"
        )

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

        yield event.chain_result(
            await self._bilibili_runtime.build_card_or_fallback_parts(notification)
        )

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

        yield event.chain_result(
            await self._bilibili_runtime.build_card_or_fallback_parts(notification)
        )

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

        yield event.chain_result(
            await self._bilibili_runtime.build_card_or_fallback_parts(notification)
        )

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
            yield event.chain_result(
                await self._bilibili_runtime.build_card_or_fallback_parts(
                    dynamic_notification
                )
            )

        video_notification = await self._bilibili_commands.build_video_test_notification(uid)
        if video_notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用视频。")
        else:
            yield event.chain_result(
                await self._bilibili_runtime.build_card_or_fallback_parts(
                    video_notification
                )
            )

        live_text, live_notification = await self._bilibili_commands.build_live_test_notification(uid)
        if live_notification is None:
            yield event.plain_result(live_text)
        else:
            yield event.chain_result(
                await self._bilibili_runtime.build_card_or_fallback_parts(
                    live_notification
                )
            )

        comment_notifications = await self._bilibili_commands.build_comment_test_notifications(uid)
        if not comment_notifications:
            yield event.plain_result(f"UID {uid} 当前最近资源下没有抓到目标评论。")
        else:
            for notification in comment_notifications:
                yield event.chain_result(
                    await self._bilibili_runtime.build_card_or_fallback_parts(
                        notification
                    )
                )

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
            yield event.chain_result(
                await self._bilibili_runtime.build_card_or_fallback_parts(notification)
            )

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
