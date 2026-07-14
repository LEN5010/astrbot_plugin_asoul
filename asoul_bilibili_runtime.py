import asyncio
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult

from asoul_bilibili import (
    COMMENT_POLL_INTERVAL_SECONDS,
    COMMENT_RESOURCE_REFRESH_INTERVAL_SECONDS,
    KV_BILIBILI_CREDENTIAL,
    KV_BILIBILI_GROUP_ORIGINS,
    KV_BILIBILI_MONITOR_STATE,
    KV_BILIBILI_PROFILE_CACHE,
    BilibiliAuthorCardProfile,
    BilibiliGateway,
    BilibiliCommentResource,
    BilibiliMonitorService,
    BilibiliRichTextNode,
    build_bilibili_push_config,
    normalize_bilibili_credential_data,
)
from asoul_bilibili_card import BilibiliCardRenderer
from asoul_core import DISPLAY_TZ

MIN_AT_ALL_REMAINING = 1
CONTENT_POLL_TIMEOUT_SECONDS = 90
CARD_RENDER_TIMEOUT_SECONDS = 30
MESSAGE_SEND_TIMEOUT_SECONDS = 30
PROFILE_CACHE_TTL_SECONDS = 6 * 60 * 60
PROFILE_FETCH_TIMEOUT_SECONDS = 20
CONTENT_POLL_STATE_KEY = "content_poll_runtime"
COMMENT_POLL_STATE_KEY = "comment_poll_runtime"
COMMENT_RESOURCE_CATALOGS_KEY = "comment_resource_catalogs"
CONTENT_UID_STATE_KEYS = frozenset(
    {
        "author_name",
        "last_dynamic_id",
        "last_dynamic_created_at",
        "recent_dynamic_ids",
        "last_live_active",
        "last_live_room_id",
    }
)
COMMENT_UID_STATE_KEYS = frozenset({"author_name", "comment_resources"})


@dataclass(frozen=True)
class BilibiliPushTarget:
    group_id: str
    platform_name: str
    unified_msg_origin: str


@dataclass(frozen=True)
class BilibiliPollError:
    category: str
    code: str
    message: str


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
        self.card_renderer = BilibiliCardRenderer(owner)
        self.task: asyncio.Task | None = None
        self.comment_task: asyncio.Task | None = None
        self.push_targets: dict[str, dict[str, str]] = {}
        self.monitor_state: dict[str, Any] = {}
        self.credential_data: dict[str, str] = {}
        self.profile_cache: dict[str, BilibiliAuthorCardProfile] = {}
        self._missing_login_logged = False
        self._runtime_initialized = False
        self._monitor_state_lock = asyncio.Lock()
        self._content_uid_poll_locks: dict[str, asyncio.Lock] = {}
        self._comment_uid_poll_locks: dict[str, asyncio.Lock] = {}
        self._content_poll_tasks: dict[str, asyncio.Task] = {}
        self._content_next_due: dict[str, float] = {}
        self._profile_refresh_tasks: dict[str, asyncio.Task] = {}
        self._profile_cache_lock = asyncio.Lock()

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
        await self._cancel_content_poll_tasks()
        await self._cancel_profile_refresh_tasks()
        await self.card_renderer.cleanup()
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
        profile_cache = await self._owner.get_kv_data(KV_BILIBILI_PROFILE_CACHE, {})
        self.push_targets = self.normalize_push_targets(push_targets)
        self.monitor_state = self.normalize_monitor_state(monitor_state)
        self.credential_data = self._resolve_credential_data(credential_data)
        self.profile_cache = self.normalize_profile_cache(profile_cache)
        self.gateway.set_credential_data(self.credential_data)

    @classmethod
    def normalize_profile_cache(
        cls,
        raw_value: Any,
    ) -> dict[str, BilibiliAuthorCardProfile]:
        if not isinstance(raw_value, dict):
            return {}
        normalized: dict[str, BilibiliAuthorCardProfile] = {}
        for raw_uid, raw_profile in raw_value.items():
            uid = str(raw_uid or "").strip()
            if not uid:
                continue
            if isinstance(raw_profile, BilibiliAuthorCardProfile):
                normalized[uid] = raw_profile
                continue
            if not isinstance(raw_profile, dict):
                continue
            normalized[uid] = BilibiliAuthorCardProfile(
                uid=str(raw_profile.get("uid") or uid),
                name=str(raw_profile.get("name") or ""),
                avatar_url=str(raw_profile.get("avatar_url") or ""),
                pendant_url=str(raw_profile.get("pendant_url") or ""),
                total_likes=cls._optional_non_negative_int(
                    raw_profile.get("total_likes")
                ),
                following=cls._optional_non_negative_int(raw_profile.get("following")),
                follower=cls._optional_non_negative_int(raw_profile.get("follower")),
                fetched_at=cls._safe_non_negative_int(raw_profile.get("fetched_at")),
            )
        return normalized

    @staticmethod
    def _optional_non_negative_int(raw_value: Any) -> Optional[int]:
        if raw_value is None or raw_value == "":
            return None
        try:
            return max(0, int(raw_value))
        except (TypeError, ValueError):
            return None

    async def get_author_card_profile(
        self,
        uid: str,
        *,
        fallback: Optional[BilibiliAuthorCardProfile] = None,
    ) -> BilibiliAuthorCardProfile:
        normalized_uid = str(uid or "").strip()
        cached = self.profile_cache.get(normalized_uid)
        now = int(time.time())
        if cached is not None and now - cached.fetched_at < PROFILE_CACHE_TTL_SECONDS:
            return cached
        if cached is not None:
            self._schedule_profile_refresh(normalized_uid)
            return cached
        try:
            return await asyncio.wait_for(
                self._refresh_author_card_profile(
                    normalized_uid,
                    fallback=fallback,
                ),
                timeout=PROFILE_FETCH_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "B 站用户资料获取失败，卡片将使用已有字段: uid=%s",
                normalized_uid,
            )
            return fallback or BilibiliAuthorCardProfile(uid=normalized_uid)

    def _schedule_profile_refresh(self, uid: str) -> None:
        existing = self._profile_refresh_tasks.get(uid)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._refresh_author_card_profile(uid))
        self._profile_refresh_tasks[uid] = task
        task.add_done_callback(
            lambda completed, refresh_uid=uid: self._finish_profile_refresh_task(
                refresh_uid,
                completed,
            )
        )

    def _finish_profile_refresh_task(self, uid: str, task: asyncio.Task) -> None:
        if self._profile_refresh_tasks.get(uid) is task:
            self._profile_refresh_tasks.pop(uid, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            logger.warning(
                "B 站用户资料后台刷新失败，继续使用旧缓存: uid=%s",
                uid,
            )

    async def _refresh_author_card_profile(
        self,
        uid: str,
        *,
        fallback: Optional[BilibiliAuthorCardProfile] = None,
    ) -> BilibiliAuthorCardProfile:
        profile = await self.gateway.get_user_card_profile(uid)
        previous = self.profile_cache.get(uid) or fallback
        if previous is not None:
            profile = BilibiliAuthorCardProfile(
                uid=profile.uid or previous.uid or uid,
                name=(
                    previous.name
                    if profile.name in {"", uid} and previous.name
                    else profile.name
                ),
                avatar_url=profile.avatar_url or previous.avatar_url,
                pendant_url=profile.pendant_url or previous.pendant_url,
                total_likes=(
                    previous.total_likes
                    if profile.total_likes is None
                    else profile.total_likes
                ),
                following=(
                    previous.following
                    if profile.following is None
                    else profile.following
                ),
                follower=(
                    previous.follower
                    if profile.follower is None
                    else profile.follower
                ),
                fetched_at=profile.fetched_at,
            )
        if profile.fetched_at <= 0:
            profile = replace(profile, fetched_at=int(time.time()))
        async with self._profile_cache_lock:
            self.profile_cache[uid] = profile
            await self.persist_profile_cache_safely()
        return profile

    async def persist_profile_cache_safely(self) -> None:
        payload = {
            uid: asdict(profile)
            for uid, profile in self.profile_cache.items()
        }
        try:
            await self._owner.put_kv_data(KV_BILIBILI_PROFILE_CACHE, payload)
        except Exception:
            logger.exception("持久化 B 站用户资料缓存失败，将保留内存缓存")

    async def _cancel_profile_refresh_tasks(self) -> None:
        tasks = [
            task
            for task in self._profile_refresh_tasks.values()
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._profile_refresh_tasks.clear()

    @staticmethod
    def build_empty_monitor_state() -> dict[str, Any]:
        return {
            "targets": {},
            "bootstrap_uids": {},
            CONTENT_POLL_STATE_KEY: {},
            COMMENT_POLL_STATE_KEY: {},
            COMMENT_RESOURCE_CATALOGS_KEY: {},
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

    @staticmethod
    def _safe_non_negative_int(raw_value: Any) -> int:
        try:
            return max(0, int(raw_value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def normalize_comment_poll_runtime(raw_value: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(raw_value, dict):
            return {}
        return {
            str(uid).strip(): deepcopy(entry)
            for uid, entry in raw_value.items()
            if str(uid or "").strip() and isinstance(entry, dict)
        }

    @staticmethod
    def normalize_comment_resource_catalogs(
        raw_value: Any,
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(raw_value, dict):
            return {}
        normalized: dict[str, dict[str, Any]] = {}
        for raw_uid, raw_entry in raw_value.items():
            uid = str(raw_uid or "").strip()
            if not uid or not isinstance(raw_entry, dict):
                continue
            resources = raw_entry.get("resources", [])
            normalized[uid] = {
                "last_attempt_at": BilibiliRuntime._safe_non_negative_int(
                    raw_entry.get("last_attempt_at")
                ),
                "last_success_at": BilibiliRuntime._safe_non_negative_int(
                    raw_entry.get("last_success_at")
                ),
                "author_name": str(raw_entry.get("author_name", "") or "").strip(),
                "resources": [
                    deepcopy(resource)
                    for resource in resources
                    if isinstance(resource, dict)
                ]
                if isinstance(resources, list)
                else [],
            }
        return normalized

    def normalize_monitor_state(self, raw_value: Any) -> dict[str, Any]:
        empty_state = self.build_empty_monitor_state()
        if not isinstance(raw_value, dict):
            return empty_state

        if (
            "targets" in raw_value
            or "bootstrap_uids" in raw_value
            or CONTENT_POLL_STATE_KEY in raw_value
            or COMMENT_POLL_STATE_KEY in raw_value
            or COMMENT_RESOURCE_CATALOGS_KEY in raw_value
        ):
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
                CONTENT_POLL_STATE_KEY: self.normalize_comment_poll_runtime(
                    raw_value.get(CONTENT_POLL_STATE_KEY, {})
                ),
                COMMENT_POLL_STATE_KEY: self.normalize_comment_poll_runtime(
                    raw_value.get(COMMENT_POLL_STATE_KEY, {})
                ),
                COMMENT_RESOURCE_CATALOGS_KEY: self.normalize_comment_resource_catalogs(
                    raw_value.get(COMMENT_RESOURCE_CATALOGS_KEY, {})
                ),
            }

        legacy_uids = self.normalize_uid_state_map(raw_value.get("uids", {}))
        if not legacy_uids:
            return empty_state

        if not self.push_targets:
            return {
                "targets": {},
                "bootstrap_uids": legacy_uids,
                CONTENT_POLL_STATE_KEY: {},
                COMMENT_POLL_STATE_KEY: self.normalize_comment_poll_runtime(
                    raw_value.get(COMMENT_POLL_STATE_KEY, {})
                ),
                COMMENT_RESOURCE_CATALOGS_KEY: {},
            }

        return {
            "targets": {
                origin: {"uids": deepcopy(legacy_uids)}
                for origin in self.push_targets
            },
            "bootstrap_uids": {},
            CONTENT_POLL_STATE_KEY: {},
            COMMENT_POLL_STATE_KEY: self.normalize_comment_poll_runtime(
                raw_value.get(COMMENT_POLL_STATE_KEY, {})
            ),
            COMMENT_RESOURCE_CATALOGS_KEY: {},
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

    def get_content_poll_entry(self, uid: str) -> dict[str, Any]:
        state = self.normalize_monitor_state(self.monitor_state)
        poll_runtime = state.setdefault(CONTENT_POLL_STATE_KEY, {})
        entry = poll_runtime.setdefault(
            str(uid),
            {
                "last_attempt_at": 0,
                "last_success_at": 0,
                "last_result": "",
                "last_duration_ms": 0,
            },
        )
        self.monitor_state = state
        return entry

    async def begin_content_poll_attempt(self, uid: str, attempted_at: int) -> None:
        async with self._monitor_state_lock:
            entry = self.get_content_poll_entry(uid)
            entry["last_attempt_at"] = max(0, int(attempted_at))
            entry["last_result"] = "running"
            await self.persist_monitor_state_safely()

    async def finish_content_poll_attempt(
        self,
        uid: str,
        *,
        finished_at: int,
        duration_ms: int,
        result: str,
        error: Optional[BilibiliPollError] = None,
    ) -> None:
        async with self._monitor_state_lock:
            entry = self.get_content_poll_entry(uid)
            entry["last_result"] = str(result)
            entry["last_duration_ms"] = max(0, int(duration_ms))
            if result == "success":
                entry["last_success_at"] = max(0, int(finished_at))
                entry.pop("last_error", None)
            elif error is not None:
                entry["last_error"] = {
                    "at": max(0, int(finished_at)),
                    "category": error.category,
                    "code": error.code,
                    "message": error.message,
                }
            await self.persist_monitor_state_safely()

    def get_comment_poll_entry(self, uid: str) -> dict[str, Any]:
        state = self.normalize_monitor_state(self.monitor_state)
        poll_runtime = state.setdefault(COMMENT_POLL_STATE_KEY, {})
        entry = poll_runtime.setdefault(
            str(uid),
            {
                "last_attempt_at": 0,
                "last_success_at": 0,
                "last_result": "",
            },
        )
        self.monitor_state = state
        return entry

    def initial_comment_poll_due_at(
        self,
        uid: str,
        *,
        monotonic_now: float,
        wall_now: int,
    ) -> float:
        entry = self.get_comment_poll_entry(uid)
        last_attempt_at = self._safe_non_negative_int(entry.get("last_attempt_at"))
        elapsed = (
            max(0, wall_now - last_attempt_at)
            if last_attempt_at
            else COMMENT_POLL_INTERVAL_SECONDS
        )
        remaining = max(0, COMMENT_POLL_INTERVAL_SECONDS - elapsed)
        return monotonic_now + remaining

    async def begin_comment_poll_attempt(self, uid: str, attempted_at: int) -> None:
        async with self._monitor_state_lock:
            entry = self.get_comment_poll_entry(uid)
            entry["last_attempt_at"] = max(0, int(attempted_at))
            await self.persist_monitor_state()

    async def finish_comment_poll_attempt(
        self,
        uid: str,
        *,
        finished_at: int,
        error: Optional[BilibiliPollError] = None,
    ) -> None:
        async with self._monitor_state_lock:
            entry = self.get_comment_poll_entry(uid)
            if error is None:
                entry["last_success_at"] = max(0, int(finished_at))
                entry["last_result"] = "success"
            else:
                entry["last_result"] = "error"
                entry["last_error"] = {
                    "at": max(0, int(finished_at)),
                    "category": error.category,
                    "code": error.code,
                    "message": error.message,
                }
            await self.persist_monitor_state_safely()

    @staticmethod
    def serialize_comment_resource(
        resource: BilibiliCommentResource,
    ) -> dict[str, Any]:
        return {
            "key": resource.key,
            "owner_uid": resource.owner_uid,
            "owner_name": resource.owner_name,
            "resource_kind": resource.resource_kind,
            "oid": resource.oid,
            "type_value": resource.type_value,
            "title": resource.title,
            "url": resource.url,
        }

    @staticmethod
    def deserialize_comment_resource(raw_value: Any) -> Optional[BilibiliCommentResource]:
        if not isinstance(raw_value, dict):
            return None
        key = str(raw_value.get("key", "") or "").strip()
        owner_uid = str(raw_value.get("owner_uid", "") or "").strip()
        resource_kind = str(raw_value.get("resource_kind", "") or "").strip()
        try:
            oid = int(raw_value.get("oid", 0) or 0)
            type_value = int(raw_value.get("type_value", 0) or 0)
        except (TypeError, ValueError):
            return None
        if not key or not owner_uid or not resource_kind or oid <= 0 or type_value <= 0:
            return None
        return BilibiliCommentResource(
            key=key,
            owner_uid=owner_uid,
            owner_name=str(raw_value.get("owner_name", "") or "").strip()
            or owner_uid,
            resource_kind=resource_kind,
            oid=oid,
            type_value=type_value,
            title=str(raw_value.get("title", "") or "").strip(),
            url=str(raw_value.get("url", "") or "").strip(),
        )

    def get_comment_resource_catalog_entry(self, uid: str) -> dict[str, Any]:
        state = self.normalize_monitor_state(self.monitor_state)
        catalogs = state.setdefault(COMMENT_RESOURCE_CATALOGS_KEY, {})
        entry = catalogs.setdefault(
            str(uid),
            {
                "last_attempt_at": 0,
                "last_success_at": 0,
                "author_name": "",
                "resources": [],
            },
        )
        self.monitor_state = state
        return entry

    def load_comment_resource_catalog(
        self, uid: str
    ) -> list[BilibiliCommentResource]:
        entry = self.get_comment_resource_catalog_entry(uid)
        resources: list[BilibiliCommentResource] = []
        for raw_resource in entry.get("resources", []):
            resource = self.deserialize_comment_resource(raw_resource)
            if resource is not None:
                resources.append(resource)
        return resources

    async def ensure_comment_resource_catalog(
        self,
        uid: str,
        author_name: str,
    ) -> tuple[str, list[BilibiliCommentResource]]:
        entry = self.get_comment_resource_catalog_entry(uid)
        now = int(time.time())
        last_attempt_at = self._safe_non_negative_int(entry.get("last_attempt_at"))
        current_resources = self.load_comment_resource_catalog(uid)
        if last_attempt_at and now - last_attempt_at < COMMENT_RESOURCE_REFRESH_INTERVAL_SECONDS:
            current_author_name = str(entry.get("author_name", "") or "").strip()
            return current_author_name or author_name or uid, current_resources

        async with self._monitor_state_lock:
            entry = self.get_comment_resource_catalog_entry(uid)
            entry["last_attempt_at"] = now
            await self.persist_monitor_state_safely()

        resolved_author_name = (
            str(entry.get("author_name", "") or "").strip()
            or str(author_name or "").strip()
        )
        try:
            if not resolved_author_name or resolved_author_name == uid:
                resolved_author_name = await self.gateway.get_comment_resource_owner_name(uid)
            resources = await self.monitor.discover_comment_resources(
                uid,
                resolved_author_name,
            )
        except Exception as exc:
            if not current_resources:
                raise
            error = self.classify_poll_error(exc)
            self.log_comment_resource_error(uid, error)
            return resolved_author_name or uid, current_resources

        async with self._monitor_state_lock:
            entry = self.get_comment_resource_catalog_entry(uid)
            entry["last_success_at"] = int(time.time())
            entry["author_name"] = resolved_author_name
            entry["resources"] = [
                self.serialize_comment_resource(resource) for resource in resources
            ]
            await self.persist_monitor_state_safely()
        return resolved_author_name, resources

    @staticmethod
    def classify_poll_error(exc: Exception) -> BilibiliPollError:
        code_value = getattr(exc, "code", None)
        status_value = getattr(exc, "status", None)
        try:
            code = int(code_value) if code_value is not None else None
        except (TypeError, ValueError):
            code = None
        try:
            status = int(status_value) if status_value is not None else None
        except (TypeError, ValueError):
            status = None

        if status == 412 or code in {-352, -412}:
            return BilibiliPollError(
                category="risk_control",
                code=str(status if status == 412 else code),
                message="请求被 B 站风控拒绝",
            )
        if status in {401, 403} or code in {-101, -102}:
            return BilibiliPollError(
                category="credential",
                code=str(status if status in {401, 403} else code),
                message="B 站登录状态无效",
            )

        exception_name = exc.__class__.__name__
        if status is not None or exception_name in {
            "NetworkException",
            "TimeoutError",
            "ClientError",
        }:
            return BilibiliPollError(
                category="network",
                code=str(status or ""),
                message="B 站网络请求失败",
            )
        if code is not None or exception_name in {
            "ResponseCodeException",
            "ApiException",
        }:
            message = str(getattr(exc, "msg", "") or "B 站接口返回错误")
            message = " ".join(message.split())[:160]
            return BilibiliPollError(
                category="api",
                code=str(code or ""),
                message=message,
            )

        message = " ".join(str(exc or exception_name).split())[:160]
        return BilibiliPollError(
            category="internal",
            code=exception_name,
            message=message or exception_name,
        )

    @staticmethod
    def log_comment_poll_error(uid: str, error: BilibiliPollError) -> None:
        log_method = logger.exception if error.category == "internal" else logger.warning
        log_method(
            "B 站评论轮询失败，状态未推进: uid=%s category=%s code=%s message=%s",
            uid,
            error.category,
            error.code or "-",
            error.message,
        )

    @staticmethod
    def log_comment_resource_error(uid: str, error: BilibiliPollError) -> None:
        logger.warning(
            "B 站评论资源发现失败，继续使用已有资源目录: uid=%s category=%s code=%s message=%s",
            uid,
            error.category,
            error.code or "-",
            error.message,
        )

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
        async with self._monitor_state_lock:
            _, state_changed = self.ensure_target_monitor_bucket(unified_msg_origin)
            if state_changed:
                await self.persist_monitor_state_safely()

    async def _run_monitor_loop(self) -> None:
        logger.info("启动 B 站自动播报任务，轮询间隔 %s 秒", self.push_config.poll_interval_seconds)
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

                for uid in list(self._content_next_due):
                    if uid not in current_uids:
                        self._content_next_due.pop(uid, None)

                for uid in current_uids:
                    self._content_next_due.setdefault(uid, now)

                for uid, task in list(self._content_poll_tasks.items()):
                    if task.done():
                        self._content_poll_tasks.pop(uid, None)

                if not current_uids:
                    await asyncio.sleep(2)
                    continue

                due_uids = [
                    uid
                    for uid in current_uids
                    if uid not in self._content_poll_tasks
                    and self._content_next_due[uid] <= now
                ]
                if not due_uids:
                    pending_due_times = [
                        self._content_next_due[uid]
                        for uid in current_uids
                        if uid not in self._content_poll_tasks
                    ]
                    if not pending_due_times:
                        await asyncio.sleep(0.2)
                        continue
                    next_due_at = min(pending_due_times)
                    await asyncio.sleep(min(max(next_due_at - now, 0.2), 2.0))
                    continue

                if now < next_dispatch_at:
                    await asyncio.sleep(min(max(next_dispatch_at - now, 0.2), 2.0))
                    continue

                run_uid = min(
                    due_uids,
                    key=lambda uid: (self._content_next_due[uid], uid),
                )
                task = asyncio.create_task(self._run_content_poll_attempt(run_uid))
                self._content_poll_tasks[run_uid] = task
                next_dispatch_at = now + self.push_config.task_gap_seconds
            except asyncio.CancelledError:
                logger.info("B 站自动播报任务已停止")
                await self._cancel_content_poll_tasks()
                raise
            except Exception:
                logger.exception("B 站自动播报任务执行异常，本轮跳过并等待下次轮询")
                await asyncio.sleep(1)

    async def _run_content_poll_attempt(self, uid: str) -> None:
        started_at = time.monotonic()
        await self.begin_content_poll_attempt(uid, int(time.time()))
        result = "success"
        error: Optional[BilibiliPollError] = None
        try:
            await asyncio.wait_for(
                self.poll_bilibili_updates_for_uid(uid),
                timeout=CONTENT_POLL_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            result = "cancelled"
            raise
        except TimeoutError:
            result = "timeout"
            error = BilibiliPollError(
                category="timeout",
                code=str(CONTENT_POLL_TIMEOUT_SECONDS),
                message="内容轮询超过时间预算",
            )
            logger.warning(
                "B 站自动播报 UID 轮询超时，本轮状态未推进: uid=%s timeout=%ss",
                uid,
                CONTENT_POLL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            result = "error"
            error = self.classify_poll_error(exc)
            logger.exception(
                "B 站自动播报 UID 轮询失败，本轮状态未推进: uid=%s",
                uid,
            )
        finally:
            duration_ms = int(max(0.0, time.monotonic() - started_at) * 1000)
            await self.finish_content_poll_attempt(
                uid,
                finished_at=int(time.time()),
                duration_ms=duration_ms,
                result=result,
                error=error,
            )
            self._content_next_due[uid] = (
                time.monotonic() + self.push_config.poll_interval_seconds
            )

    async def _cancel_content_poll_tasks(self) -> None:
        tasks = [task for task in self._content_poll_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._content_poll_tasks.clear()

    async def _run_comment_monitor_loop(self) -> None:
        logger.info("启动 B 站评论自动播报任务，轮询间隔 %s 秒", COMMENT_POLL_INTERVAL_SECONDS)
        uid_states: dict[str, float] = {}

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
                wall_now = int(time.time())

                for uid in list(uid_states):
                    if uid not in current_uids:
                        uid_states.pop(uid, None)

                for uid in current_uids:
                    if uid not in uid_states:
                        uid_states[uid] = self.initial_comment_poll_due_at(
                            uid,
                            monotonic_now=now,
                            wall_now=wall_now,
                        )

                due_uids = [uid for uid in current_uids if uid_states[uid] <= now]
                if not due_uids:
                    next_due_at = min(uid_states[uid] for uid in current_uids)
                    await asyncio.sleep(min(max(next_due_at - now, 0.2), 2.0))
                    continue

                run_uid = min(due_uids, key=lambda uid: (uid_states[uid], uid))
                try:
                    await self.begin_comment_poll_attempt(
                        run_uid,
                        int(time.time()),
                    )
                    await self.poll_bilibili_comments_for_uid(run_uid)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = self.classify_poll_error(exc)
                    await self.finish_comment_poll_attempt(
                        run_uid,
                        finished_at=int(time.time()),
                        error=error,
                    )
                    self.log_comment_poll_error(run_uid, error)
                else:
                    await self.finish_comment_poll_attempt(
                        run_uid,
                        finished_at=int(time.time()),
                    )
                finally:
                    finished_at = time.monotonic()
                    uid_states[run_uid] = (
                        finished_at + COMMENT_POLL_INTERVAL_SECONDS
                    )
            except asyncio.CancelledError:
                logger.info("B 站评论自动播报任务已停止")
                raise
            except Exception:
                logger.exception("B 站评论自动播报任务执行异常，本轮跳过并等待下次轮询")
                await asyncio.sleep(1)

    @staticmethod
    def _get_or_create_uid_lock(
        locks: dict[str, asyncio.Lock], uid: str
    ) -> asyncio.Lock:
        normalized_uid = str(uid)
        lock = locks.get(normalized_uid)
        if lock is None:
            lock = asyncio.Lock()
            locks[normalized_uid] = lock
        return lock

    async def poll_bilibili_updates_for_uid(self, uid: str) -> None:
        lock = self._get_or_create_uid_lock(self._content_uid_poll_locks, uid)
        async with lock:
            await self._poll_bilibili_updates_for_uid(uid)

    async def _poll_bilibili_updates_for_uid(self, uid: str) -> None:
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
        fallback_profile = next(
            (
                post.author
                for post in snapshot.dynamics
                if post.author.name or post.author.avatar_url
            ),
            BilibiliAuthorCardProfile(uid=uid, name=snapshot.author_name),
        )
        author_profile = await self.get_author_card_profile(
            uid,
            fallback=fallback_profile,
        )
        snapshot = replace(snapshot, author_profile=author_profile)

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
                state_domain="content",
            )

    async def poll_bilibili_comments_for_uid(self, uid: str) -> None:
        lock = self._get_or_create_uid_lock(self._comment_uid_poll_locks, uid)
        async with lock:
            await self._poll_bilibili_comments_for_uid(uid)

    async def _poll_bilibili_comments_for_uid(self, uid: str) -> None:
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

        seed_author_name = ""
        if isinstance(snapshot_seed_state, dict):
            seed_author_name = str(
                snapshot_seed_state.get("author_name", "") or ""
            ).strip()
        author_name, comment_resources = await self.ensure_comment_resource_catalog(
            uid,
            seed_author_name,
        )
        if not comment_resources:
            return

        snapshot = await self.monitor.fetch_comment_snapshot(
            uid=uid,
            author_name=author_name,
            comment_resources=comment_resources,
            previous_state=snapshot_seed_state,
        )

        for resource_key, exc in snapshot.resource_errors.items():
            error = self.classify_poll_error(exc)
            logger.warning(
                "B 站评论资源扫描失败，状态未推进: uid=%s resource=%s category=%s code=%s message=%s",
                uid,
                resource_key,
                error.category,
                error.code or "-",
                error.message,
            )
        if snapshot.resource_errors and not snapshot.comment_posts:
            raise next(iter(snapshot.resource_errors.values()))

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
                state_domain="comment",
            )

    async def apply_delivery_plan_to_target(
        self,
        target: BilibiliPushTarget,
        uid: str,
        plan: Any,
        snapshot: Any,
        state_domain: str,
    ) -> None:
        for delivery in plan.deliveries:
            try:
                result = await self.build_notification_result(
                    delivery.notification, target
                )
                await asyncio.wait_for(
                    self.context.send_message(target.unified_msg_origin, result),
                    timeout=MESSAGE_SEND_TIMEOUT_SECONDS,
                )
            except Exception:
                logger.exception(
                    "发送 B 站播报失败: target=%s uid=%s kind=%s",
                    target.unified_msg_origin,
                    snapshot.uid,
                    delivery.notification.kind,
                )
                return

            await self.commit_target_uid_state(
                target.unified_msg_origin,
                uid,
                self.extract_uid_state_domain(delivery.uid_state, state_domain),
            )

        await self.commit_target_uid_state(
            target.unified_msg_origin,
            uid,
            self.extract_uid_state_domain(plan.final_state, state_domain),
            only_if_changed=True,
        )

    @staticmethod
    def extract_uid_state_domain(
        uid_state: Any,
        state_domain: str,
    ) -> dict[str, Any]:
        source = uid_state if isinstance(uid_state, dict) else {}
        allowed_keys = (
            COMMENT_UID_STATE_KEYS
            if state_domain == "comment"
            else CONTENT_UID_STATE_KEYS
        )
        return {
            key: deepcopy(source[key])
            for key in allowed_keys
            if key in source
        }

    async def commit_target_uid_state(
        self,
        unified_msg_origin: str,
        uid: str,
        uid_state: dict[str, Any],
        *,
        only_if_changed: bool = False,
    ) -> None:
        async with self._monitor_state_lock:
            target_state, _ = self.ensure_target_monitor_bucket(unified_msg_origin)
            uid_state_map = target_state.setdefault("uids", {})
            current_state = deepcopy(uid_state_map.get(uid, {}))
            next_state = deepcopy(current_state)
            next_state.update(deepcopy(uid_state))
            if only_if_changed and current_state == next_state:
                return
            uid_state_map[uid] = next_state
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
        chain_parts = await self.build_card_or_fallback_parts(notification)
        if notification.kind == "live" and await self.should_send_live_atall(target):
            chain_parts = [Comp.AtAll(), Comp.Plain(self.safe_plain_newline())] + chain_parts
        return MessageEventResult(chain=chain_parts).use_t2i(False)

    async def build_card_or_fallback_parts(self, notification: Any) -> list[Any]:
        if (
            notification.kind in {"dynamic", "video", "live"}
            and self.push_config.render_bilibili_cards
        ):
            try:
                card_path = await asyncio.wait_for(
                    self.card_renderer.render(notification),
                    timeout=CARD_RENDER_TIMEOUT_SECONDS,
                )
                return [
                    Comp.Image.fromFileSystem(card_path),
                    Comp.Plain(str(notification.url or "")),
                ]
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "B 站卡片渲染失败，回退旧消息格式: uid=%s kind=%s",
                    getattr(notification, "uid", ""),
                    getattr(notification, "kind", ""),
                )
        return self.build_notification_parts(notification)

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

    async def build_bilibili_status_text(self) -> str:
        await self.ensure_ready()
        state = self.normalize_monitor_state(self.monitor_state)
        content_runtime = state[CONTENT_POLL_STATE_KEY]
        comment_runtime = state[COMMENT_POLL_STATE_KEY]
        active_targets = self.get_active_push_targets()
        lines = [
            "【B站推送状态】",
            f"自动播报：{'已启用' if self.push_config.enabled else '未启用'}",
            f"评论推送：{'已启用' if self.push_config.push_comment else '未启用'}",
            f"登录状态：{'已登录' if self.gateway.has_credential() else '未登录'}",
            f"请求客户端：{self.push_config.request_client}",
            f"内容任务：{'运行中' if self.task and not self.task.done() else '未运行'}；在途 {len(self._content_poll_tasks)}",
            f"评论任务：{'运行中' if self.comment_task and not self.comment_task.done() else '未运行'}",
            f"已登记目标群：{len(active_targets)}",
            f"评论轮询间隔：{COMMENT_POLL_INTERVAL_SECONDS} 秒",
            f"评论资源发现间隔：{COMMENT_RESOURCE_REFRESH_INTERVAL_SECONDS} 秒",
        ]
        for uid in self.push_config.target_uids:
            content_entry = content_runtime.get(uid, {})
            content_attempt_at = self._safe_non_negative_int(
                content_entry.get("last_attempt_at")
            )
            if content_attempt_at:
                content_result = str(content_entry.get("last_result", "") or "未知")
                duration_ms = self._safe_non_negative_int(
                    content_entry.get("last_duration_ms")
                )
                inflight_text = (
                    "；当前在途"
                    if uid in self._content_poll_tasks
                    and not self._content_poll_tasks[uid].done()
                    else ""
                )
                lines.append(
                    f"UID {uid} 内容：最近 {self.format_status_time(content_attempt_at)}；"
                    f"结果 {content_result}；耗时 {duration_ms / 1000:.2f} 秒"
                    f"{inflight_text}"
                )
            else:
                inflight_text = (
                    "；当前在途"
                    if uid in self._content_poll_tasks
                    and not self._content_poll_tasks[uid].done()
                    else ""
                )
                lines.append(f"UID {uid} 内容：尚未轮询{inflight_text}")

            entry = comment_runtime.get(uid, {})
            last_attempt_at = self._safe_non_negative_int(entry.get("last_attempt_at"))
            if not last_attempt_at:
                lines.append(f"UID {uid} 评论：尚未轮询")
                continue
            last_result = str(entry.get("last_result", "") or "")
            next_poll_at = last_attempt_at + COMMENT_POLL_INTERVAL_SECONDS
            detail = (
                f"UID {uid} 评论：最近轮询 {self.format_status_time(last_attempt_at)}；"
                f"结果 {last_result or '未知'}；"
                f"下次最早 {self.format_status_time(next_poll_at)}"
            )
            if last_result == "error":
                error = entry.get("last_error", {})
                if isinstance(error, dict):
                    category = str(error.get("category", "") or "unknown")
                    code = str(error.get("code", "") or "-")
                    message = str(error.get("message", "") or "")
                    detail += f"；错误 {category}/{code} {message}".rstrip()
            lines.append(detail)
        return "\n".join(lines)

    @staticmethod
    def format_status_time(timestamp: int) -> str:
        return datetime.fromtimestamp(int(timestamp), DISPLAY_TZ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

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
