import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult

from asoul_bilibili import (
    COMMENT_POLL_INTERVAL_SECONDS,
    KV_BILIBILI_CREDENTIAL,
    KV_BILIBILI_GROUP_ORIGINS,
    KV_BILIBILI_MONITOR_STATE,
    BilibiliGateway,
    BilibiliMonitorService,
    BilibiliRichTextNode,
    build_bilibili_push_config,
    normalize_bilibili_credential_data,
)
from asoul_core import DISPLAY_TZ

MIN_AT_ALL_REMAINING = 1


@dataclass(frozen=True)
class BilibiliPushTarget:
    group_id: str
    platform_name: str
    unified_msg_origin: str


class BilibiliRuntime:
    def __init__(self, owner: Any, context: Any, config: Any) -> None:
        self._owner = owner
        self.context = context
        self.config = config or {}
        self.push_config = build_bilibili_push_config(self.config)
        self.gateway = BilibiliGateway(
            request_client=self.push_config.request_client,
            credential_data=self.push_config.credential_data,
        )
        self.monitor = BilibiliMonitorService(self.gateway)
        self.task: asyncio.Task | None = None
        self.comment_task: asyncio.Task | None = None
        self.push_targets: dict[str, dict[str, str]] = {}
        self.monitor_state: dict[str, Any] = {}
        self.credential_data: dict[str, str] = {}
        self._missing_login_logged = False
        self._runtime_initialized = False

    async def ensure_ready(self) -> None:
        self.refresh_config()
        if not self._runtime_initialized:
            await self.load_state()
            self._runtime_initialized = True

        if not self.push_config.enabled or not self.push_config.target_uids:
            return

        if (
            self.push_config.push_dynamic
            or self.push_config.push_video
            or self.push_config.push_live
        ) and (not self.task or self.task.done()):
            self.task = asyncio.create_task(self._run_monitor_loop())

        if self.push_config.push_comment and (
            not self.comment_task or self.comment_task.done()
        ):
            self.comment_task = asyncio.create_task(self._run_comment_monitor_loop())

    async def terminate(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.comment_task and not self.comment_task.done():
            self.comment_task.cancel()
            try:
                await self.comment_task
            except asyncio.CancelledError:
                pass
        self._runtime_initialized = False

    def refresh_config(self) -> None:
        previous_request_client = self.push_config.request_client
        self.push_config = build_bilibili_push_config(self.config)
        if self.push_config.request_client != previous_request_client:
            self.gateway.set_request_client(self.push_config.request_client)
        self.gateway.set_credential_data(
            self._resolve_credential_data(self.credential_data)
        )

    async def load_state(self) -> None:
        push_targets = await self._owner.get_kv_data(KV_BILIBILI_GROUP_ORIGINS, {})
        monitor_state = await self._owner.get_kv_data(KV_BILIBILI_MONITOR_STATE, {})
        credential_data = await self._owner.get_kv_data(KV_BILIBILI_CREDENTIAL, {})
        self.push_targets = self.normalize_push_targets(push_targets)
        self.monitor_state = self.normalize_monitor_state(monitor_state)
        self.credential_data = self._resolve_credential_data(credential_data)
        self.gateway.set_credential_data(self.credential_data)

    @staticmethod
    def build_empty_monitor_state() -> dict[str, Any]:
        return {
            "targets": {},
            "bootstrap_uids": {},
        }

    @staticmethod
    def normalize_uid_state_map(raw_value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw_value, dict):
            return {}

        normalized: dict[str, dict[str, Any]] = {}
        for raw_uid, raw_state in raw_value.items():
            uid = str(raw_uid or "").strip()
            if not uid or not isinstance(raw_state, dict):
                continue
            normalized[uid] = deepcopy(raw_state)
        return normalized

    def normalize_monitor_state(self, raw_value: Any) -> dict[str, Any]:
        empty_state = self.build_empty_monitor_state()
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
                        "uids": self.normalize_uid_state_map(
                            raw_target_state.get("uids", {})
                        )
                    }

            return {
                "targets": normalized_targets,
                "bootstrap_uids": self.normalize_uid_state_map(
                    raw_value.get("bootstrap_uids", {})
                ),
            }

        legacy_uids = self.normalize_uid_state_map(raw_value.get("uids", {}))
        if not legacy_uids:
            return empty_state

        if not self.push_targets:
            return {
                "targets": {},
                "bootstrap_uids": legacy_uids,
            }

        return {
            "targets": {
                origin: {"uids": deepcopy(legacy_uids)}
                for origin in self.push_targets
            },
            "bootstrap_uids": {},
        }

    def ensure_target_monitor_bucket(
        self, unified_msg_origin: str
    ) -> tuple[dict[str, Any], bool]:
        state = self.normalize_monitor_state(self.monitor_state)
        targets = state.setdefault("targets", {})
        origin = str(unified_msg_origin or "").strip()
        if not origin:
            self.monitor_state = state
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
            target_state["uids"] = self.normalize_uid_state_map(
                target_state.get("uids", {})
            )

        self.monitor_state = state
        return target_state, changed

    async def persist_monitor_state(self) -> None:
        await self._owner.put_kv_data(KV_BILIBILI_MONITOR_STATE, self.monitor_state)

    async def persist_monitor_state_safely(self) -> bool:
        try:
            await self.persist_monitor_state()
            return True
        except Exception:
            logger.exception("持久化 B 站监控状态失败，将继续使用内存态运行")
            return False

    async def remember_group_origin(self, event: AstrMessageEvent) -> None:
        await self.ensure_ready()
        group_id = str(getattr(event.message_obj, "group_id", "") or "").strip()
        if not group_id:
            return
        if group_id not in self.push_config.group_whitelist:
            return

        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not unified_msg_origin:
            return
        platform_name = self.extract_platform_name(unified_msg_origin)
        if not platform_name:
            return

        current_target = self.push_targets.get(unified_msg_origin)
        if current_target and current_target.get("group_id") == group_id:
            return

        next_targets = {
            origin: target
            for origin, target in self.push_targets.items()
            if str(target.get("group_id", "") or "").strip() != group_id
        }
        next_targets[unified_msg_origin] = {
            "group_id": group_id,
            "platform_name": platform_name,
            "unified_msg_origin": unified_msg_origin,
        }
        self.push_targets = next_targets
        await self._owner.put_kv_data(KV_BILIBILI_GROUP_ORIGINS, self.push_targets)
        _, state_changed = self.ensure_target_monitor_bucket(unified_msg_origin)
        if state_changed:
            await self.persist_monitor_state_safely()

    async def _run_monitor_loop(self) -> None:
        logger.info("启动 B 站自动播报任务，轮询间隔 %s 秒", self.push_config.poll_interval_seconds)
        uid_states: dict[str, float] = {}
        next_dispatch_at = 0.0

        while True:
            try:
                self.refresh_config()
                if not self.gateway.has_credential():
                    if not self._missing_login_logged:
                        logger.warning("B 站自动播报未登录，轮询已暂停。请配置凭据或使用 /bili_login 登录。")
                        self._missing_login_logged = True
                    await asyncio.sleep(self.push_config.poll_interval_seconds)
                    continue

                self._missing_login_logged = False
                current_uids = list(self.push_config.target_uids)
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
                try:
                    await self.poll_bilibili_updates_for_uid(run_uid)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "B 站自动播报 UID 轮询失败，本轮状态未推进: uid=%s",
                        run_uid,
                    )
                finally:
                    finished_at = time.monotonic()
                    uid_states[run_uid] = (
                        finished_at + self.push_config.poll_interval_seconds
                    )
                    next_dispatch_at = finished_at + self.push_config.task_gap_seconds
            except asyncio.CancelledError:
                logger.info("B 站自动播报任务已停止")
                raise
            except Exception:
                logger.exception("B 站自动播报任务执行异常，本轮跳过并等待下次轮询")
                await asyncio.sleep(1)

    async def _run_comment_monitor_loop(self) -> None:
        logger.info("启动 B 站评论自动播报任务，轮询间隔 %s 秒", COMMENT_POLL_INTERVAL_SECONDS)
        uid_states: dict[str, float] = {}
        next_dispatch_at = 0.0

        while True:
            try:
                self.refresh_config()
                if (
                    not self.push_config.enabled
                    or not self.push_config.push_comment
                    or not self.push_config.target_uids
                ):
                    await asyncio.sleep(COMMENT_POLL_INTERVAL_SECONDS)
                    continue

                if not self.gateway.has_credential():
                    if not self._missing_login_logged:
                        logger.warning("B 站自动播报未登录，评论轮询已暂停。请配置凭据或使用 /bili_login 登录。")
                        self._missing_login_logged = True
                    await asyncio.sleep(COMMENT_POLL_INTERVAL_SECONDS)
                    continue

                self._missing_login_logged = False
                current_uids = list(self.push_config.target_uids)
                now = time.monotonic()

                for uid in list(uid_states):
                    if uid not in current_uids:
                        uid_states.pop(uid, None)

                for uid in current_uids:
                    uid_states.setdefault(uid, now)

                due_uids = [uid for uid in current_uids if uid_states[uid] <= now]
                if not due_uids:
                    next_due_at = min(uid_states[uid] for uid in current_uids)
                    await asyncio.sleep(min(max(next_due_at - now, 0.2), 2.0))
                    continue

                if now < next_dispatch_at:
                    await asyncio.sleep(min(max(next_dispatch_at - now, 0.2), 2.0))
                    continue

                run_uid = min(due_uids, key=lambda uid: (uid_states[uid], uid))
                try:
                    await self.poll_bilibili_comments_for_uid(run_uid)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "B 站评论自动播报 UID 轮询失败，本轮状态未推进: uid=%s",
                        run_uid,
                    )
                finally:
                    finished_at = time.monotonic()
                    uid_states[run_uid] = (
                        finished_at + COMMENT_POLL_INTERVAL_SECONDS
                    )
                    next_dispatch_at = finished_at + self.push_config.task_gap_seconds
            except asyncio.CancelledError:
                logger.info("B 站评论自动播报任务已停止")
                raise
            except Exception:
                logger.exception("B 站评论自动播报任务执行异常，本轮跳过并等待下次轮询")
                await asyncio.sleep(1)

    async def poll_bilibili_updates_for_uid(self, uid: str) -> None:
        target_entries = self.get_active_push_targets()
        if not target_entries:
            logger.info("存在 B 站新通知，但当前没有已登记的白名单群")
            return

        snapshot_seed_state: dict[str, Any] | None = None
        for target in target_entries:
            target_state, _ = self.ensure_target_monitor_bucket(
                target.unified_msg_origin
            )
            candidate_state = target_state.get("uids", {}).get(uid)
            if isinstance(candidate_state, dict) and candidate_state:
                snapshot_seed_state = deepcopy(candidate_state)
                break

        snapshot = await self.monitor.fetch_uid_snapshot(
            config=self.push_config,
            uid=uid,
            previous_state=snapshot_seed_state,
        )

        for target in target_entries:
            target_state, _ = self.ensure_target_monitor_bucket(
                target.unified_msg_origin
            )
            target_uid_state = target_state.setdefault("uids", {}).get(uid, {})
            plan = self.monitor.plan_uid_deliveries(
                config=self.push_config,
                previous_state=target_uid_state,
                snapshot=snapshot,
            )
            await self.apply_delivery_plan_to_target(
                target=target,
                uid=uid,
                plan=plan,
                snapshot=snapshot,
            )

    async def poll_bilibili_comments_for_uid(self, uid: str) -> None:
        target_entries = self.get_active_push_targets()
        if not target_entries:
            logger.info("存在 B 站新评论通知，但当前没有已登记的白名单群")
            return

        snapshot_seed_state: dict[str, Any] | None = None
        for target in target_entries:
            target_state, _ = self.ensure_target_monitor_bucket(
                target.unified_msg_origin
            )
            candidate_state = target_state.get("uids", {}).get(uid)
            if isinstance(candidate_state, dict) and candidate_state:
                snapshot_seed_state = deepcopy(candidate_state)
                break

        snapshot = await self.monitor.fetch_comment_snapshot(
            config=self.push_config,
            uid=uid,
            previous_state=snapshot_seed_state,
        )

        for target in target_entries:
            target_state, _ = self.ensure_target_monitor_bucket(
                target.unified_msg_origin
            )
            target_uid_state = target_state.setdefault("uids", {}).get(uid, {})
            plan = self.monitor.plan_comment_deliveries(
                config=self.push_config,
                previous_state=target_uid_state,
                snapshot=snapshot,
            )
            await self.apply_delivery_plan_to_target(
                target=target,
                uid=uid,
                plan=plan,
                snapshot=snapshot,
            )

    async def apply_delivery_plan_to_target(
        self,
        target: BilibiliPushTarget,
        uid: str,
        plan: Any,
        snapshot: Any,
    ) -> None:
        target_state, _ = self.ensure_target_monitor_bucket(
            target.unified_msg_origin
        )
        uid_state_map = target_state.setdefault("uids", {})
        current_uid_state = deepcopy(uid_state_map.get(uid, {}))

        for delivery in plan.deliveries:
            try:
                result = await self.build_notification_result(
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
            await self.persist_monitor_state_safely()

        if current_uid_state == plan.final_state:
            return

        uid_state_map[uid] = deepcopy(plan.final_state)
        await self.persist_monitor_state_safely()

    def get_active_push_targets(self) -> list[BilibiliPushTarget]:
        targets: list[BilibiliPushTarget] = []
        allowed_groups = set(self.push_config.group_whitelist)
        for unified_msg_origin, raw_target in self.push_targets.items():
            group_id = str(raw_target.get("group_id", "") or "").strip()
            if not group_id or group_id not in allowed_groups:
                continue
            platform_name = str(raw_target.get("platform_name", "") or "").strip()
            if not platform_name:
                platform_name = self.extract_platform_name(unified_msg_origin)
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

    async def build_notification_result(
        self,
        notification: Any,
        target: BilibiliPushTarget,
    ) -> MessageEventResult:
        chain_parts = self.build_notification_parts(notification)
        if notification.kind == "live" and await self.should_send_live_atall(target):
            chain_parts = [Comp.AtAll(), Comp.Plain(" ")] + chain_parts
        return MessageEventResult(chain=chain_parts).use_t2i(False)

    @staticmethod
    def safe_plain_newline() -> str:
        return "\u200b\n\u200b"

    def build_notification_parts(self, notification: Any) -> list[Any]:
        prefix_map = {
            "dynamic": "【B站动态】",
            "video": "【B站新视频】",
            "live": "【B站开播】",
            "comment": "【B站评论】",
        }
        prefix = prefix_map.get(notification.kind, "【B站通知】")
        chain_parts: list[Any] = [Comp.Plain(f"{prefix}{notification.author_name}")]

        if notification.kind == "dynamic":
            chain_parts.append(Comp.Plain(self.safe_plain_newline()))
            self.append_rich_text_parts(chain_parts, notification.rich_nodes, notification.text)
            for image_url in notification.image_urls:
                chain_parts.append(Comp.Plain(self.safe_plain_newline()))
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
                f"{prefix}{notification.author_name}{self.safe_plain_newline()}"
                + self.safe_plain_newline().join(detail_parts)
            )
            for image_url in notification.image_urls:
                chain_parts.append(Comp.Plain(self.safe_plain_newline()))
                chain_parts.append(Comp.Image.fromURL(image_url))
        else:
            title = str(notification.title or "").strip()
            if title:
                chain_parts[0] = Comp.Plain(
                    f"{prefix}{notification.author_name}{self.safe_plain_newline()}{title}"
                )
            cover_url = str(notification.cover_url or "").strip()
            if cover_url:
                chain_parts.append(Comp.Plain(self.safe_plain_newline()))
                chain_parts.append(Comp.Image.fromURL(cover_url))

        chain_parts.append(Comp.Plain(f"{self.safe_plain_newline()}{notification.url}"))
        return chain_parts

    def append_rich_text_parts(
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

    def _resolve_credential_data(self, runtime_credential_data: Any) -> dict[str, str]:
        runtime_data = normalize_bilibili_credential_data(runtime_credential_data)
        if runtime_data:
            return runtime_data
        return normalize_bilibili_credential_data(self.push_config.credential_data)

    def normalize_push_targets(self, raw_value: Any) -> dict[str, dict[str, str]]:
        if not isinstance(raw_value, dict):
            return {}

        normalized: dict[str, dict[str, str]] = {}
        for raw_key, raw_target in raw_value.items():
            if isinstance(raw_target, str):
                unified_msg_origin = str(raw_target or "").strip()
                group_id = str(raw_key or "").strip()
                platform_name = self.extract_platform_name(unified_msg_origin)
            elif isinstance(raw_target, dict):
                unified_msg_origin = str(
                    raw_target.get("unified_msg_origin", raw_key) or ""
                ).strip()
                group_id = str(raw_target.get("group_id", "") or "").strip()
                platform_name = str(raw_target.get("platform_name", "") or "").strip()
                if not platform_name:
                    platform_name = self.extract_platform_name(unified_msg_origin)
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

    async def save_credential(self, credential_data: dict[str, str]) -> None:
        normalized = normalize_bilibili_credential_data(credential_data)
        self.credential_data = normalized
        self.gateway.set_credential_data(normalized)
        await self._owner.put_kv_data(KV_BILIBILI_CREDENTIAL, normalized)

    async def clear_credential(self) -> None:
        self.credential_data = {}
        self.gateway.clear_credential()
        await self._owner.delete_kv_data(KV_BILIBILI_CREDENTIAL)

    async def ensure_private_bili_command(self, event: AstrMessageEvent) -> Optional[str]:
        await self.ensure_ready()
        if event.message_obj.group_id:
            return "请在私聊中使用这个指令。"
        if not self.gateway.has_credential():
            return "当前未登录 B 站，请先使用 /bili_login。"
        return None

    async def send_atall_test(self, event: AstrMessageEvent) -> Optional[str]:
        group_id = str(getattr(event.message_obj, "group_id", "") or "").strip()
        if not group_id:
            return "请在目标群聊中使用 /bili_test_atall。"

        unified_msg_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not unified_msg_origin:
            return "当前群聊上下文缺少 unified_msg_origin，无法测试 @全体。"

        platform_name = self.extract_platform_name(unified_msg_origin)
        if not platform_name:
            return "当前群聊平台识别失败，无法测试 @全体。"

        target = BilibiliPushTarget(
            group_id=group_id,
            platform_name=platform_name,
            unified_msg_origin=unified_msg_origin,
        )
        if not await self.should_send_live_atall(target):
            return "当前群不满足 @全体发送条件，请查看插件日志。"

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
        return None

    async def should_send_live_atall(self, target: BilibiliPushTarget) -> bool:
        platform_inst = None
        if hasattr(self.context, "get_platform_inst"):
            platform_inst = self.context.get_platform_inst(target.platform_name)
        if not platform_inst:
            platform_inst = self.find_platform_by_name(target.platform_name)
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

        remain_data = self.extract_action_data(remain_raw)
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

    def find_platform_by_name(self, platform_name: str) -> Optional[Any]:
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
    def extract_platform_name(unified_msg_origin: str) -> str:
        try:
            platform_name, _, _ = unified_msg_origin.split(":", 2)
        except ValueError:
            return ""
        return str(platform_name or "").strip()

    @staticmethod
    def extract_action_data(action_result: Any) -> dict[str, Any]:
        if not isinstance(action_result, dict):
            return {}
        payload = action_result.get("data")
        if isinstance(payload, dict):
            return payload
        return action_result
