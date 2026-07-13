import asyncio
import json
import sys
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
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
    THIS_WEEK_TRIGGER_TEXTS,
    TODAY_TRIGGER_TEXTS,
    TOMORROW_TRIGGER_TEXTS,
)
from asoul_render import ScheduleImageRenderer
from asoul_schedule import ScheduleService

MIN_AT_ALL_REMAINING = 1
QR_CODE_PATH = Path(__file__).resolve().parent / "temp" / "bilibili_login_qrcode.png"
DEBUG_PAYLOAD_DIR = Path(__file__).resolve().parent / "temp" / "debug_payloads"


@dataclass(frozen=True)
class CommentTestResource:
    key: str
    owner_uid: str
    owner_name: str
    resource_kind: str
    oid: int
    type_value: int
    title: str
    url: str


@dataclass(frozen=True)
class BilibiliPushTarget:
    group_id: str
    platform_name: str
    unified_msg_origin: str


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
        self._bilibili_push_targets: dict[str, dict[str, str]] = {}
        self._bilibili_monitor_state: dict = {}
        self._bilibili_credential_data: dict[str, str] = {}
        self._bilibili_missing_login_logged = False
        self._bilibili_runtime_initialized = False

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        await self._ensure_bilibili_runtime_ready()

    def _refresh_bilibili_config(self) -> None:
        previous_request_client = self._bilibili_config.request_client
        self._bilibili_config = build_bilibili_push_config(self.config)
        if self._bilibili_config.request_client != previous_request_client:
            self._bilibili_gateway.set_request_client(self._bilibili_config.request_client)
        self._bilibili_gateway.set_credential_data(
            self._resolve_bilibili_credential_data(self._bilibili_credential_data)
        )

    async def _ensure_bilibili_runtime_ready(self) -> None:
        self._refresh_bilibili_config()
        if not self._bilibili_runtime_initialized:
            await self._load_bilibili_runtime_state()
            self._bilibili_runtime_initialized = True

        if not self._bilibili_config.enabled:
            return
        if not self._bilibili_config.target_uids:
            return

        if self._bilibili_task and not self._bilibili_task.done():
            return

        self._bilibili_task = asyncio.create_task(self._run_bilibili_monitor_loop())

    @staticmethod
    def _build_empty_bilibili_monitor_state() -> dict[str, Any]:
        return {
            "targets": {},
            "bootstrap_uids": {},
        }

    @staticmethod
    def _normalize_bilibili_uid_state_map(raw_value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw_value, dict):
            return {}

        normalized: dict[str, dict[str, Any]] = {}
        for raw_uid, raw_state in raw_value.items():
            uid = str(raw_uid or "").strip()
            if not uid or not isinstance(raw_state, dict):
                continue
            normalized[uid] = deepcopy(raw_state)
        return normalized

    def _normalize_bilibili_monitor_state(self, raw_value: Any) -> dict[str, Any]:
        empty_state = self._build_empty_bilibili_monitor_state()
        if not isinstance(raw_value, dict):
            return empty_state

        if "targets" in raw_value or "bootstrap_uids" in raw_value:
            raw_targets = raw_value.get("targets", {})
            normalized_targets: dict[str, dict[str, Any]] = {}
            if isinstance(raw_targets, dict):
                for raw_origin, raw_target_state in raw_targets.items():
                    origin = str(raw_origin or "").strip()
                    if not origin or not isinstance(raw_target_state, dict):
                        continue
                    normalized_targets[origin] = {
                        "uids": self._normalize_bilibili_uid_state_map(
                            raw_target_state.get("uids", {})
                        )
                    }

            return {
                "targets": normalized_targets,
                "bootstrap_uids": self._normalize_bilibili_uid_state_map(
                    raw_value.get("bootstrap_uids", {})
                ),
            }

        legacy_uids = self._normalize_bilibili_uid_state_map(raw_value.get("uids", {}))
        if not legacy_uids:
            return empty_state

        if not self._bilibili_push_targets:
            return {
                "targets": {},
                "bootstrap_uids": legacy_uids,
            }

        return {
            "targets": {
                origin: {"uids": deepcopy(legacy_uids)}
                for origin in self._bilibili_push_targets
            },
            "bootstrap_uids": {},
        }

    def _ensure_bilibili_target_monitor_bucket(
        self, unified_msg_origin: str
    ) -> tuple[dict[str, Any], bool]:
        state = self._normalize_bilibili_monitor_state(self._bilibili_monitor_state)
        targets = state.setdefault("targets", {})
        origin = str(unified_msg_origin or "").strip()
        if not origin:
            self._bilibili_monitor_state = state
            return {"uids": {}}, False

        changed = False
        target_state = targets.get(origin)
        if not isinstance(target_state, dict):
            seed_uids = {}
            if not targets:
                seed_uids = deepcopy(state.get("bootstrap_uids", {}))
            target_state = {"uids": seed_uids}
            targets[origin] = target_state
            changed = True
        else:
            target_state["uids"] = self._normalize_bilibili_uid_state_map(
                target_state.get("uids", {})
            )

        self._bilibili_monitor_state = state
        return target_state, changed

    async def _persist_bilibili_monitor_state(self) -> None:
        await self.put_kv_data(KV_BILIBILI_MONITOR_STATE, self._bilibili_monitor_state)

    async def _persist_bilibili_monitor_state_safely(self) -> bool:
        try:
            await self._persist_bilibili_monitor_state()
            return True
        except Exception:
            logger.exception("持久化 B 站监控状态失败，将继续使用内存态运行")
            return False

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def remember_group_origin(self, event: AstrMessageEvent):
        await self._ensure_bilibili_runtime_ready()
        group_id = str(getattr(event.message_obj, "group_id", "") or "").strip()
        if not group_id:
            return
        if group_id not in self._bilibili_config.group_whitelist:
            return

        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not unified_msg_origin:
            return
        platform_name = self._extract_platform_name(unified_msg_origin)
        if not platform_name:
            return

        current_target = self._bilibili_push_targets.get(unified_msg_origin)
        if current_target and current_target.get("group_id") == group_id:
            return

        next_targets = {
            origin: target
            for origin, target in self._bilibili_push_targets.items()
            if str(target.get("group_id", "") or "").strip() != group_id
        }
        next_targets[unified_msg_origin] = {
            "group_id": group_id,
            "platform_name": platform_name,
            "unified_msg_origin": unified_msg_origin,
        }
        self._bilibili_push_targets = next_targets
        await self.put_kv_data(KV_BILIBILI_GROUP_ORIGINS, self._bilibili_push_targets)
        _, state_changed = self._ensure_bilibili_target_monitor_bucket(unified_msg_origin)
        if state_changed:
            await self._persist_bilibili_monitor_state_safely()

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
        message_text = event.message_str.strip()
        if (
            message_text not in TODAY_TRIGGER_TEXTS
            and message_text not in TOMORROW_TRIGGER_TEXTS
            and message_text not in THIS_WEEK_TRIGGER_TEXTS
        ):
            return

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

            day_items = [
                (target_day, self._schedule_service.build_schedule_items(day_events.get(target_day, [])))
                for target_day in sorted(day_events)
            ]
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
        self._bilibili_runtime_initialized = False
        return None

    async def _load_bilibili_runtime_state(self) -> None:
        push_targets = await self.get_kv_data(KV_BILIBILI_GROUP_ORIGINS, {})
        monitor_state = await self.get_kv_data(KV_BILIBILI_MONITOR_STATE, {})
        credential_data = await self.get_kv_data(KV_BILIBILI_CREDENTIAL, {})
        self._bilibili_push_targets = self._normalize_bilibili_push_targets(push_targets)
        self._bilibili_monitor_state = self._normalize_bilibili_monitor_state(monitor_state)
        self._bilibili_credential_data = self._resolve_bilibili_credential_data(credential_data)
        self._bilibili_gateway.set_credential_data(self._bilibili_credential_data)

    async def _run_bilibili_monitor_loop(self) -> None:
        logger.info("启动 B 站自动播报任务，轮询间隔 %s 秒", self._bilibili_config.poll_interval_seconds)
        uid_states: dict[str, float] = {}
        next_dispatch_at = 0.0

        while True:
            try:
                self._refresh_bilibili_config()
                if not self._bilibili_gateway.has_credential():
                    if not self._bilibili_missing_login_logged:
                        logger.warning("B 站自动播报未登录，轮询已暂停。请配置凭据或使用 /bili_login 登录。")
                        self._bilibili_missing_login_logged = True
                    await asyncio.sleep(self._bilibili_config.poll_interval_seconds)
                    continue

                self._bilibili_missing_login_logged = False
                current_uids = list(self._bilibili_config.target_uids)
                now = time.monotonic()

                for uid in list(uid_states):
                    if uid not in current_uids:
                        uid_states.pop(uid, None)

                for uid in current_uids:
                    uid_states.setdefault(uid, now)

                if not current_uids:
                    await asyncio.sleep(2)
                    continue

                due_uids = [uid for uid in current_uids if uid_states[uid] <= now]
                if not due_uids:
                    next_due_at = min(uid_states[uid] for uid in current_uids)
                    await asyncio.sleep(min(max(next_due_at - now, 0.2), 2.0))
                    continue

                if now < next_dispatch_at:
                    await asyncio.sleep(min(max(next_dispatch_at - now, 0.2), 2.0))
                    continue

                run_uid = min(due_uids, key=lambda uid: (uid_states[uid], uid))
                await self._poll_bilibili_updates_for_uid(run_uid)

                finished_at = time.monotonic()
                uid_states[run_uid] = finished_at + self._bilibili_config.poll_interval_seconds
                next_dispatch_at = finished_at + self._bilibili_config.task_gap_seconds
            except asyncio.CancelledError:
                logger.info("B 站自动播报任务已停止")
                raise
            except Exception:
                logger.exception("B 站自动播报任务执行异常，本轮跳过并等待下次轮询")
                await asyncio.sleep(1)

    async def _poll_bilibili_updates_for_uid(self, uid: str) -> None:
        target_entries = self._get_active_push_targets()
        if not target_entries:
            logger.info("存在 B 站新通知，但当前没有已登记的白名单群")
            return

        snapshot_seed_state: dict[str, Any] | None = None
        for target in target_entries:
            target_state, _ = self._ensure_bilibili_target_monitor_bucket(
                target.unified_msg_origin
            )
            candidate_state = target_state.get("uids", {}).get(uid)
            if isinstance(candidate_state, dict) and candidate_state:
                snapshot_seed_state = deepcopy(candidate_state)
                break

        snapshot = await self._bilibili_monitor.fetch_uid_snapshot(
            config=self._bilibili_config,
            uid=uid,
            previous_state=snapshot_seed_state,
        )

        for target in target_entries:
            target_state, _ = self._ensure_bilibili_target_monitor_bucket(
                target.unified_msg_origin
            )
            target_uid_state = target_state.setdefault("uids", {}).get(uid, {})
            plan = self._bilibili_monitor.plan_uid_deliveries(
                config=self._bilibili_config,
                previous_state=target_uid_state,
                snapshot=snapshot,
            )
            await self._apply_bilibili_delivery_plan_to_target(
                target=target,
                uid=uid,
                plan=plan,
                snapshot=snapshot,
            )

    async def _apply_bilibili_delivery_plan_to_target(
        self,
        target: BilibiliPushTarget,
        uid: str,
        plan,
        snapshot,
    ) -> None:
        target_state, _ = self._ensure_bilibili_target_monitor_bucket(
            target.unified_msg_origin
        )
        uid_state_map = target_state.setdefault("uids", {})
        current_uid_state = deepcopy(uid_state_map.get(uid, {}))

        for delivery in plan.deliveries:
            try:
                result = await self._build_notification_result(
                    delivery.notification, target
                )
                await self.context.send_message(target.unified_msg_origin, result)
            except Exception:
                logger.exception(
                    "发送 B 站播报失败: target=%s uid=%s kind=%s",
                    target.unified_msg_origin,
                    snapshot.uid,
                    delivery.notification.kind,
                )
                return

            current_uid_state = deepcopy(delivery.uid_state)
            uid_state_map[uid] = deepcopy(current_uid_state)
            await self._persist_bilibili_monitor_state_safely()

        if current_uid_state == plan.final_state:
            return

        uid_state_map[uid] = deepcopy(plan.final_state)
        await self._persist_bilibili_monitor_state_safely()

    def _get_active_push_targets(self) -> list[BilibiliPushTarget]:
        targets: list[BilibiliPushTarget] = []
        allowed_groups = set(self._bilibili_config.group_whitelist)
        for unified_msg_origin, raw_target in self._bilibili_push_targets.items():
            group_id = str(raw_target.get("group_id", "") or "").strip()
            if not group_id or group_id not in allowed_groups:
                continue
            platform_name = str(raw_target.get("platform_name", "") or "").strip()
            if not platform_name:
                platform_name = self._extract_platform_name(unified_msg_origin)
            if not platform_name:
                logger.warning("跳过无法识别平台的群播报目标: group_id=%s umo=%s", group_id, unified_msg_origin)
                continue
            targets.append(
                BilibiliPushTarget(
                    group_id=group_id,
                    platform_name=platform_name,
                    unified_msg_origin=unified_msg_origin,
                )
            )
        return targets

    async def _build_notification_result(
        self,
        notification,
        target: BilibiliPushTarget,
    ) -> MessageEventResult:
        chain_parts = self._build_notification_parts(notification)
        if notification.kind == "live" and await self._should_send_live_atall(target):
            chain_parts = [Comp.AtAll(), Comp.Plain(" ")] + chain_parts
        return MessageEventResult(chain=chain_parts).use_t2i(False)

    @staticmethod
    def _safe_plain_newline() -> str:
        return "\u200b\n\u200b"

    def _build_notification_parts(self, notification) -> list[Any]:
        prefix_map = {
            "dynamic": "【B站动态】",
            "video": "【B站新视频】",
            "live": "【B站开播】",
            "comment": "【B站评论】",
        }
        prefix = prefix_map.get(notification.kind, "【B站通知】")
        chain_parts: list[Any] = [Comp.Plain(f"{prefix}{notification.author_name}")]

        if notification.kind == "dynamic":
            chain_parts.append(Comp.Plain(self._safe_plain_newline()))
            self._append_rich_text_parts(chain_parts, notification.rich_nodes, notification.text)
            for image_url in notification.image_urls:
                chain_parts.append(Comp.Plain(self._safe_plain_newline()))
                chain_parts.append(Comp.Image.fromURL(image_url))
        elif notification.kind == "comment":
            detail_parts: list[str] = []
            timestamp = int(getattr(notification, "comment_created_at", 0) or 0)
            if timestamp > 0:
                detail_parts.append(
                    f"{notification.author_name}于{datetime.fromtimestamp(timestamp, DISPLAY_TZ).strftime('%Y-%m-%d %H:%M')}"
                )
            resource_owner_name = str(
                getattr(notification, "comment_resource_owner_name", "") or ""
            ).strip()
            resource_kind = str(
                getattr(notification, "comment_resource_kind", "") or "内容"
            ).strip() or "内容"
            resource_title = str(
                getattr(notification, "comment_resource_title", "") or ""
            ).strip()
            action_text = str(
                getattr(notification, "comment_action_text", "") or "发表了评论"
            ).strip() or "发表了评论"
            context_text = (
                f"在{resource_owner_name}的{resource_kind}"
                if resource_owner_name
                else f"在该{resource_kind}"
            )
            if resource_title:
                context_text += f"《{resource_title}》"
            detail_parts.append(f"{context_text}下{action_text}：")
            comment_text = str(notification.text or "").strip()
            if comment_text:
                detail_parts.append(comment_text)
            chain_parts[0] = Comp.Plain(
                f"{prefix}{notification.author_name}{self._safe_plain_newline()}"
                + self._safe_plain_newline().join(detail_parts)
            )
            for image_url in notification.image_urls:
                chain_parts.append(Comp.Plain(self._safe_plain_newline()))
                chain_parts.append(Comp.Image.fromURL(image_url))
        else:
            title = str(notification.title or "").strip()
            if title:
                chain_parts[0] = Comp.Plain(
                    f"{prefix}{notification.author_name}{self._safe_plain_newline()}{title}"
                )
            cover_url = str(notification.cover_url or "").strip()
            if cover_url:
                chain_parts.append(Comp.Plain(self._safe_plain_newline()))
                chain_parts.append(Comp.Image.fromURL(cover_url))

        chain_parts.append(Comp.Plain(f"{self._safe_plain_newline()}{notification.url}"))
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

    def _normalize_bilibili_push_targets(self, raw_value: Any) -> dict[str, dict[str, str]]:
        if not isinstance(raw_value, dict):
            return {}

        normalized: dict[str, dict[str, str]] = {}
        for raw_key, raw_target in raw_value.items():
            if isinstance(raw_target, str):
                unified_msg_origin = str(raw_target or "").strip()
                group_id = str(raw_key or "").strip()
                platform_name = self._extract_platform_name(unified_msg_origin)
            elif isinstance(raw_target, dict):
                unified_msg_origin = str(
                    raw_target.get("unified_msg_origin", raw_key) or ""
                ).strip()
                group_id = str(raw_target.get("group_id", "") or "").strip()
                platform_name = str(raw_target.get("platform_name", "") or "").strip()
                if not platform_name:
                    platform_name = self._extract_platform_name(unified_msg_origin)
            else:
                continue

            if not unified_msg_origin or not group_id or not platform_name:
                continue

            normalized[unified_msg_origin] = {
                "group_id": group_id,
                "platform_name": platform_name,
                "unified_msg_origin": unified_msg_origin,
            }
        return normalized

    async def _save_bilibili_credential(self, credential_data: dict[str, str]) -> None:
        normalized = normalize_bilibili_credential_data(credential_data)
        self._bilibili_credential_data = normalized
        self._bilibili_gateway.set_credential_data(normalized)
        await self.put_kv_data(KV_BILIBILI_CREDENTIAL, normalized)

    async def _clear_bilibili_credential(self) -> None:
        self._bilibili_credential_data = {}
        self._bilibili_gateway.clear_credential()
        await self.delete_kv_data(KV_BILIBILI_CREDENTIAL)

    def _write_debug_payload_file(self, kind: str, uid: str, payload: dict[str, Any]) -> Path:
        DEBUG_PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(DISPLAY_TZ).strftime("%Y%m%d_%H%M%S")
        file_path = DEBUG_PAYLOAD_DIR / f"{kind}_{uid}_{timestamp}.json"
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return file_path

    async def _ensure_private_bili_command(self, event: AstrMessageEvent) -> Optional[str]:
        await self._ensure_bilibili_runtime_ready()
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

    async def _build_comment_test_notifications(self, uid: str) -> list[BilibiliNotification]:
        owner_name = await self._bilibili_gateway.get_user_name(uid)
        recent_dynamics = await self._bilibili_gateway.get_recent_dynamics(uid, stop_at_id=None)
        recent_videos = await self._bilibili_gateway.get_recent_videos(uid, stop_at_id=None)
        resources = self._build_comment_test_resources(
            uid,
            owner_name,
            recent_dynamics[:2],
            recent_videos[:2],
        )
        notifications: list[BilibiliNotification] = []
        watched_uids = {target_uid for target_uid in self._bilibili_config.target_uids}
        for resource in resources:
            comments = await self._bilibili_gateway.get_recent_comments(resource)
            filtered_comments = [
                comment_post
                for comment_post in comments
                if comment_post.author_uid in watched_uids
            ]
            for comment_post in sorted(filtered_comments, key=lambda item: (item.created_at, self._safe_int(item.id))):
                notifications.append(
                    self._build_comment_test_notification(resource, comment_post)
                )
        return notifications

    @staticmethod
    def _safe_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _build_comment_test_resources(
        self,
        owner_uid: str,
        owner_name: str,
        dynamics,
        videos,
    ) -> list[CommentTestResource]:
        resources: list[CommentTestResource] = []
        seen_keys = set()

        def append_resource(resource: CommentTestResource) -> None:
            if resource.key in seen_keys:
                return
            seen_keys.add(resource.key)
            resources.append(resource)

        for post in dynamics:
            if getattr(post, "is_video_dynamic", False):
                continue
            if getattr(post, "comment_oid", 0) <= 0 or getattr(post, "comment_type", 0) <= 0:
                continue
            append_resource(
                CommentTestResource(
                    key=f"dynamic:{post.comment_type}:{post.comment_oid}",
                    owner_uid=owner_uid,
                    owner_name=owner_name,
                    resource_kind="dynamic",
                    oid=post.comment_oid,
                    type_value=post.comment_type,
                    title=self._trim_plain_text(post.text, 80),
                    url=post.url,
                )
            )

        for post in videos:
            if getattr(post, "comment_oid", 0) <= 0:
                continue
            append_resource(
                CommentTestResource(
                    key=f"video:{post.comment_oid}",
                    owner_uid=owner_uid,
                    owner_name=owner_name,
                    resource_kind="video",
                    oid=post.comment_oid,
                    type_value=1,
                    title=self._trim_plain_text(post.title, 80),
                    url=post.url,
                )
            )

        return resources

    def _build_comment_test_notification(self, resource: CommentTestResource, comment_post) -> BilibiliNotification:
        resource_text = "动态" if resource.resource_kind == "dynamic" else "视频"
        action_text = "回复了评论" if comment_post.is_reply else "发表了评论"
        return BilibiliNotification(
            kind="comment",
            uid=comment_post.author_uid,
            author_name=comment_post.author_name,
            title="",
            url=resource.url,
            text=comment_post.text,
            image_urls=list(getattr(comment_post, "image_urls", []) or []),
            comment_created_at=getattr(comment_post, "created_at", 0),
            comment_resource_owner_name=resource.owner_name,
            comment_resource_kind=resource_text,
            comment_resource_title=resource.title,
            comment_action_text=action_text,
        )

    @staticmethod
    def _trim_plain_text(text: str, limit: int) -> str:
        compact = " ".join(str(text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 1)].rstrip() + "…"

    @staticmethod
    def _normalize_command_uid(uid: str) -> str:
        normalized_uid = str(uid or "").strip()
        if not normalized_uid or not normalized_uid.isdigit():
            raise ValueError("B站 UID 必须为纯数字字符串")
        return normalized_uid

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_dynamic")
    async def bili_test_dynamic(self, event: AstrMessageEvent, uid: str):
        error_text = await self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        notification = await self._build_dynamic_test_notification(uid)
        if notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用动态。")
            return

        yield event.chain_result(self._build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_dump_dynamic")
    async def bili_dump_dynamic(self, event: AstrMessageEvent, uid: str):
        error_text = await self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        payload = await self._bilibili_gateway.get_raw_dynamics_page(uid, offset="")
        file_path = self._write_debug_payload_file(
            "dynamic",
            uid,
            {
                "uid": uid,
                "captured_at": datetime.now(DISPLAY_TZ).isoformat(),
                "payload": payload,
            },
        )
        yield event.plain_result(f"已导出动态原始 payload: {file_path}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_video")
    async def bili_test_video(self, event: AstrMessageEvent, uid: str):
        error_text = await self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        notification = await self._build_video_test_notification(uid)
        if notification is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到可用视频。")
            return

        yield event.chain_result(self._build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_live")
    async def bili_test_live(self, event: AstrMessageEvent, uid: str):
        error_text = await self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        live_status = await self._bilibili_gateway.get_live_status_by_uid(uid)
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
    @filter.command("bili_dump_live")
    async def bili_dump_live(self, event: AstrMessageEvent, uid: str):
        error_text = await self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        payload = await self._bilibili_gateway.get_raw_live_info(uid)
        file_path = self._write_debug_payload_file(
            "live",
            uid,
            {
                "uid": uid,
                "captured_at": datetime.now(DISPLAY_TZ).isoformat(),
                "payload": payload,
            },
        )
        yield event.plain_result(f"已导出直播原始 payload: {file_path}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_atall")
    async def bili_test_atall(self, event: AstrMessageEvent):
        group_id = str(getattr(event.message_obj, "group_id", "") or "").strip()
        if not group_id:
            yield event.plain_result("请在目标群聊中使用 /bili_test_atall。")
            return

        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not unified_msg_origin:
            yield event.plain_result("当前群聊上下文缺少 unified_msg_origin，无法测试 @全体。")
            return

        platform_name = self._extract_platform_name(unified_msg_origin)
        if not platform_name:
            yield event.plain_result("当前群聊平台识别失败，无法测试 @全体。")
            return

        target = BilibiliPushTarget(
            group_id=group_id,
            platform_name=platform_name,
            unified_msg_origin=unified_msg_origin,
        )
        if not await self._should_send_live_atall(target):
            yield event.plain_result("当前群不满足 @全体发送条件，请查看插件日志。")
            return

        await self.context.send_message(
            unified_msg_origin,
            MessageEventResult(
                chain=[
                    Comp.AtAll(),
                    Comp.Plain(" "),
                    Comp.Plain("【B站开播测试】这是一条 @全体 功能测试消息。"),
                ]
            ).use_t2i(False),
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_all")
    async def bili_test_all(self, event: AstrMessageEvent, uid: str):
        error_text = await self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
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

        live_status = await self._bilibili_gateway.get_live_status_by_uid(uid)
        if live_status is None:
            yield event.plain_result(f"UID {uid} 当前没有抓到直播间信息。")
        else:
            author_name = await self._bilibili_gateway.get_user_name(uid)
            if not live_status.is_live:
                yield event.plain_result(
                    f"【B站直播状态】{author_name}\n当前未开播\n{live_status.url}"
                )
            else:
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

        comment_notifications = await self._build_comment_test_notifications(uid)
        if not comment_notifications:
            yield event.plain_result(f"UID {uid} 当前最近资源下没有抓到目标评论。")
        else:
            for notification in comment_notifications:
                yield event.chain_result(self._build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_test_comment")
    async def bili_test_comment(self, event: AstrMessageEvent, uid: str):
        error_text = await self._ensure_private_bili_command(event)
        if error_text:
            yield event.plain_result(error_text)
            return
        try:
            uid = self._normalize_command_uid(uid)
        except ValueError:
            yield event.plain_result("UID 格式错误，请输入纯数字 UID。")
            return

        notifications = await self._build_comment_test_notifications(uid)
        if not notifications:
            yield event.plain_result(f"UID {uid} 当前最近资源下没有抓到目标评论。")
            return
        for notification in notifications:
            yield event.chain_result(self._build_notification_parts(notification))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("bili_login")
    async def bili_login(self, event: AstrMessageEvent):
        await self._ensure_bilibili_runtime_ready()
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
        await self._ensure_bilibili_runtime_ready()
        await self._clear_bilibili_credential()
        yield event.plain_result("已清除当前保存的 B 站登录态。")

    async def _should_send_live_atall(self, target: BilibiliPushTarget) -> bool:
        platform_inst = None
        if hasattr(self.context, "get_platform_inst"):
            platform_inst = self.context.get_platform_inst(target.platform_name)
        if not platform_inst:
            platform_inst = self._find_platform_by_name(target.platform_name)
        if not platform_inst:
            logger.warning("直播 @全体失败：找不到平台实例 %s", target.platform_name)
            return False

        if not hasattr(platform_inst, "get_client"):
            logger.warning("直播 @全体失败：平台 %s 不支持 get_client", target.platform_name)
            return False

        client = platform_inst.get_client()
        if not client or not hasattr(client, "call_action"):
            logger.warning("直播 @全体失败：平台 %s 不支持 call_action", target.platform_name)
            return False

        try:
            group_id_param: int | str = int(target.group_id) if target.group_id.isdigit() else target.group_id
            remain_raw = await client.call_action(
                "get_group_at_all_remain",
                group_id=group_id_param,
            )
        except Exception:
            logger.exception("查询群 %s @全体剩余次数失败", target.group_id)
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
            logger.info("群 %s 当前不允许 @全体成员", target.group_id)
            return False
        if group_remain < MIN_AT_ALL_REMAINING or self_remain < MIN_AT_ALL_REMAINING:
            logger.info(
                "群 %s @全体次数不足: group=%s, self=%s",
                target.group_id,
                group_remain,
                self_remain,
            )
            return False
        return True

    def _find_platform_by_name(self, platform_name: str) -> Optional[Any]:
        platform_manager = getattr(self.context, "platform_manager", None)
        if platform_manager is None or not hasattr(platform_manager, "get_insts"):
            return None

        normalized_platform_name = str(platform_name or "").strip().lower()
        if not normalized_platform_name:
            return None

        for platform in platform_manager.get_insts():
            metadata = getattr(platform, "metadata", None)
            candidate_names: list[str] = []

            if isinstance(metadata, dict):
                candidate_names.extend(
                    [
                        str(metadata.get("id", "") or "").strip().lower(),
                        str(metadata.get("type", "") or "").strip().lower(),
                        str(metadata.get("name", "") or "").strip().lower(),
                    ]
                )
            elif metadata is not None:
                candidate_names.extend(
                    [
                        str(getattr(metadata, "id", "") or "").strip().lower(),
                        str(getattr(metadata, "type", "") or "").strip().lower(),
                        str(getattr(metadata, "name", "") or "").strip().lower(),
                    ]
                )

            candidate_names.extend(
                [
                    str(getattr(platform, "id", "") or "").strip().lower(),
                    str(getattr(platform, "platform_id", "") or "").strip().lower(),
                    str(getattr(platform, "name", "") or "").strip().lower(),
                ]
            )

            if normalized_platform_name in {name for name in candidate_names if name}:
                return platform
        return None

    @staticmethod
    def _extract_platform_name(unified_msg_origin: str) -> str:
        try:
            platform_name, _, _ = unified_msg_origin.split(":", 2)
        except ValueError:
            return ""
        return str(platform_name or "").strip()

    @staticmethod
    def _extract_action_data(action_result: Any) -> dict[str, Any]:
        if not isinstance(action_result, dict):
            return {}
        payload = action_result.get("data")
        if isinstance(payload, dict):
            return payload
        return action_result
