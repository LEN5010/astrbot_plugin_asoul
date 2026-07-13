import logging
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

DEFAULT_BILIBILI_TARGET_UIDS = [
    "672328094",
    "672342685",
    "3537115310721181",
    "3537115310721781",
    "672353429",
    "703007996",
    "3493085336046382",
]
DEFAULT_POLL_INTERVAL_SECONDS = 120
MIN_POLL_INTERVAL_SECONDS = 30
BILIBILI_CREDENTIAL_FIELDS = (
    "sessdata",
    "bili_jct",
    "buvid3",
    "buvid4",
    "dedeuserid",
    "ac_time_value",
)

KV_BILIBILI_MONITOR_STATE = "bilibili_monitor_state"
KV_BILIBILI_GROUP_ORIGINS = "bilibili_group_origins"
KV_BILIBILI_CREDENTIAL = "bilibili_credential"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BilibiliPushConfig:
    enabled: bool
    poll_interval_seconds: int
    group_whitelist: List[str]
    target_uids: List[str]
    push_dynamic: bool
    push_video: bool
    push_live: bool
    request_client: str
    credential_data: Dict[str, str]


@dataclass(frozen=True)
class BilibiliRichTextNode:
    kind: str
    text: str = ""
    image_url: str = ""


@dataclass(frozen=True)
class BilibiliDynamicPost:
    id: str
    text: str
    url: str
    rich_nodes: List[BilibiliRichTextNode] = field(default_factory=list)
    image_urls: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class BilibiliVideoPost:
    id: str
    title: str
    url: str
    cover_url: str = ""


@dataclass(frozen=True)
class BilibiliLiveStatus:
    is_live: bool
    title: str
    room_id: str
    url: str
    cover_url: str = ""


@dataclass(frozen=True)
class BilibiliNotification:
    kind: str
    uid: str
    author_name: str
    title: str
    url: str
    text: str = ""
    rich_nodes: List[BilibiliRichTextNode] = field(default_factory=list)
    image_urls: List[str] = field(default_factory=list)
    cover_url: str = ""


def build_bilibili_push_config(raw_config: Optional[Dict[str, Any]]) -> BilibiliPushConfig:
    source = raw_config or {}
    poll_interval = int(source.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS) or DEFAULT_POLL_INTERVAL_SECONDS)
    request_client = str(source.get("request_client", "aiohttp") or "aiohttp").strip().lower()
    if request_client not in {"aiohttp", "httpx", "curl_cffi"}:
        request_client = "aiohttp"

    return BilibiliPushConfig(
        enabled=bool(source.get("enabled", False)),
        poll_interval_seconds=max(MIN_POLL_INTERVAL_SECONDS, poll_interval),
        group_whitelist=_normalize_string_list(source.get("group_whitelist", [])),
        target_uids=_normalize_string_list(source.get("target_uids", DEFAULT_BILIBILI_TARGET_UIDS)),
        push_dynamic=bool(source.get("push_dynamic", True)),
        push_video=bool(source.get("push_video", True)),
        push_live=bool(source.get("push_live", True)),
        request_client=request_client,
        credential_data=_normalize_credential_data(source),
    )


def _normalize_string_list(raw_value: Any) -> List[str]:
    if not isinstance(raw_value, list):
        return []

    normalized: List[str] = []
    seen = set()
    for item in raw_value:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _normalize_credential_data(raw_value: Any) -> Dict[str, str]:
    if isinstance(raw_value, dict) and any(key in raw_value for key in BILIBILI_CREDENTIAL_FIELDS):
        source = raw_value
    elif isinstance(raw_value, dict):
        source = {}
    else:
        source = {}

    normalized: Dict[str, str] = {}
    for field_name in BILIBILI_CREDENTIAL_FIELDS:
        value = source.get(field_name, "")
        text = str(value or "").strip()
        if text:
            normalized[field_name] = text
    return normalized


def normalize_bilibili_credential_data(raw_value: Any) -> Dict[str, str]:
    return _normalize_credential_data(raw_value)


class BilibiliGateway:
    def __init__(
        self,
        request_client: str = "aiohttp",
        credential_data: Optional[Dict[str, str]] = None,
    ) -> None:
        self._request_client = request_client
        self._client_selected = False
        self._credential_data = _normalize_credential_data(credential_data or {})
        self._credential = None
        if self._credential_data:
            self._credential = self._build_credential(self._credential_data)

    def _load_modules(self):
        from bilibili_api import Credential, select_client, user

        if not self._client_selected:
            select_client(self._request_client)
            self._client_selected = True
        return user, Credential

    def _build_credential(self, credential_data: Dict[str, str]):
        if not credential_data.get("sessdata"):
            return None
        _, credential_cls = self._load_modules()
        return credential_cls(**credential_data)

    def set_credential_data(self, credential_data: Optional[Dict[str, str]]) -> None:
        self._credential_data = _normalize_credential_data(credential_data or {})
        self._credential = self._build_credential(self._credential_data) if self._credential_data else None

    def clear_credential(self) -> None:
        self._credential_data = {}
        self._credential = None

    def get_credential_data(self) -> Dict[str, str]:
        return dict(self._credential_data)

    def has_credential(self) -> bool:
        return bool(self._credential and self._credential.has_sessdata())

    def _new_user(self, uid: str):
        user_module, _ = self._load_modules()
        kwargs: Dict[str, Any] = {"uid": int(uid)}
        if self._credential is not None:
            kwargs["credential"] = self._credential
        return user_module.User(**kwargs)

    async def get_user_name(self, uid: str) -> str:
        user_obj = self._new_user(uid)
        info = await user_obj.get_user_info()

        for key in ("name", "uname", "nickname"):
            value = info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        return uid

    async def get_recent_dynamics(
        self,
        uid: str,
        stop_at_id: Optional[str],
    ) -> List[BilibiliDynamicPost]:
        user_obj = self._new_user(uid)

        offset = ""
        collected: List[BilibiliDynamicPost] = []
        seen_ids = set()

        while True:
            page = await user_obj.get_dynamics_new(offset=offset)
            items = self._extract_dynamic_items(page)
            if not items:
                break

            reached_stop = False
            for item in items:
                if self._is_pinned_dynamic(item):
                    continue
                parsed = self._parse_dynamic_post(item)
                if parsed is None or parsed.id in seen_ids:
                    continue
                if stop_at_id and parsed.id == stop_at_id:
                    reached_stop = True
                    break
                seen_ids.add(parsed.id)
                collected.append(parsed)

            if reached_stop:
                break
            if stop_at_id is None and collected:
                break

            next_offset = page.get("offset") or page.get("next_offset") or ""
            if not next_offset or str(next_offset) == str(offset):
                break
            offset = str(next_offset)

        return collected

    async def get_recent_videos(
        self,
        uid: str,
        stop_at_id: Optional[str],
    ) -> List[BilibiliVideoPost]:
        user_obj = self._new_user(uid)

        page_index = 1
        collected: List[BilibiliVideoPost] = []
        seen_ids = set()

        while True:
            page = await user_obj.get_videos(pn=page_index, ps=30)
            items = self._extract_video_items(page)
            if not items:
                break

            reached_stop = False
            for item in items:
                parsed = self._parse_video_post(item)
                if parsed is None or parsed.id in seen_ids:
                    continue
                if stop_at_id and parsed.id == stop_at_id:
                    reached_stop = True
                    break
                seen_ids.add(parsed.id)
                collected.append(parsed)

            if reached_stop or (stop_at_id is None and collected) or len(items) < 30:
                break

            page_index += 1

        return collected

    async def get_live_status(self, uid: str) -> Optional[BilibiliLiveStatus]:
        user_obj = self._new_user(uid)
        info = await user_obj.get_live_info()

        live_status_value = self._find_first_value(info, ("live_status", "liveStatus", "roomStatus"))
        if live_status_value is None:
            return None

        room_id_value = self._find_first_value(info, ("roomid", "room_id", "roomId"))
        title_value = self._find_first_value(info, ("title", "roomtitle"))
        url_value = self._find_first_value(info, ("url", "link"))
        cover_value = self._find_first_value(info, ("cover_from_user", "user_cover", "cover", "keyframe"))

        room_id = str(room_id_value).strip() if room_id_value is not None else ""
        url = _normalize_url(str(url_value).strip() if url_value is not None else "")
        if not url and room_id:
            url = f"https://live.bilibili.com/{room_id}"

        title = str(title_value).strip() if title_value is not None else "直播已开始"

        try:
            is_live = int(live_status_value) == 1
        except Exception:
            is_live = str(live_status_value).strip() == "1"

        return BilibiliLiveStatus(
            is_live=is_live,
            title=title or "直播已开始",
            room_id=room_id,
            url=url or "https://live.bilibili.com",
            cover_url=_normalize_url(str(cover_value).strip() if cover_value is not None else ""),
        )

    def _extract_dynamic_items(self, page: Dict[str, Any]) -> List[Dict[str, Any]]:
        for key in ("items", "cards", "list"):
            value = page.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _extract_video_items(self, page: Dict[str, Any]) -> List[Dict[str, Any]]:
        list_value = page.get("list")
        if isinstance(list_value, dict):
            for key in ("vlist", "list"):
                value = list_value.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        if isinstance(list_value, list):
            return [item for item in list_value if isinstance(item, dict)]
        for key in ("vlist", "items"):
            value = page.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    def _is_pinned_dynamic(self, item: Dict[str, Any]) -> bool:
        modules = item.get("modules", {})
        if not isinstance(modules, dict):
            return False
        module_tag = modules.get("module_tag", {})
        if not isinstance(module_tag, dict):
            return False
        return str(module_tag.get("text", "") or "").strip() == "置顶"

    def _parse_dynamic_post(self, item: Dict[str, Any]) -> Optional[BilibiliDynamicPost]:
        dynamic_id = item.get("id_str") or item.get("id")
        if dynamic_id is None:
            dynamic_id = self._find_first_value(item, ("id_str", "dynamic_id", "dynamicId", "id"))
        if dynamic_id is None:
            return None

        rich_nodes, plain_text = self._extract_dynamic_rich_nodes(item)
        image_urls = self._extract_dynamic_image_urls(item)

        url_value = (
            self._find_first_value(item.get("basic", {}), ("jump_url",))
            or item.get("jump_url")
            or item.get("url")
            or self._find_first_value(item, ("jump_url", "url"))
        )
        url = _normalize_url(str(url_value).strip() if url_value is not None else "")
        if not url:
            url = f"https://t.bilibili.com/{dynamic_id}"

        text = plain_text or "发布了新动态"
        return BilibiliDynamicPost(
            id=str(dynamic_id),
            text=text,
            url=url,
            rich_nodes=rich_nodes,
            image_urls=image_urls,
        )

    def _extract_dynamic_rich_nodes(self, item: Dict[str, Any]) -> tuple[List[BilibiliRichTextNode], str]:
        modules = item.get("modules", {}) if isinstance(item.get("modules"), dict) else {}
        module_dynamic = modules.get("module_dynamic", {}) if isinstance(modules.get("module_dynamic"), dict) else {}
        desc = module_dynamic.get("desc", {}) if isinstance(module_dynamic.get("desc"), dict) else {}
        major = module_dynamic.get("major", {}) if isinstance(module_dynamic.get("major"), dict) else {}
        opus = major.get("opus", {}) if isinstance(major.get("opus"), dict) else {}
        summary = opus.get("summary", {}) if isinstance(opus.get("summary"), dict) else {}

        raw_nodes = summary.get("rich_text_nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raw_nodes = desc.get("rich_text_nodes")
        if not isinstance(raw_nodes, list):
            raw_nodes = []

        nodes: List[BilibiliRichTextNode] = []
        plain_parts: List[str] = []
        for raw_node in raw_nodes:
            if not isinstance(raw_node, dict):
                continue
            emoji = raw_node.get("emoji", {}) if isinstance(raw_node.get("emoji"), dict) else {}
            emoji_url = _normalize_url(str(emoji.get("icon_url", "") or ""))
            node_text = str(raw_node.get("text", "") or emoji.get("text", "") or "")
            if emoji_url:
                nodes.append(BilibiliRichTextNode(kind="emoji", text=node_text, image_url=emoji_url))
                if node_text:
                    plain_parts.append(node_text)
                continue
            if node_text:
                nodes.append(BilibiliRichTextNode(kind="text", text=node_text))
                plain_parts.append(node_text)

        summary_text = str(summary.get("text", "") or desc.get("text", "") or "").strip()
        plain_text = "".join(plain_parts).strip() or summary_text or self._extract_nested_text(major) or self._extract_nested_text(item)
        if not nodes and plain_text:
            nodes = [BilibiliRichTextNode(kind="text", text=plain_text)]

        return nodes, plain_text.strip()

    def _extract_dynamic_image_urls(self, item: Dict[str, Any]) -> List[str]:
        modules = item.get("modules", {}) if isinstance(item.get("modules"), dict) else {}
        module_dynamic = modules.get("module_dynamic", {}) if isinstance(modules.get("module_dynamic"), dict) else {}
        major = module_dynamic.get("major", {}) if isinstance(module_dynamic.get("major"), dict) else {}
        image_urls: List[str] = []
        seen = set()

        def append_candidate(raw_value: Any) -> None:
            url = _normalize_url(str(raw_value or "").strip())
            if not url or url in seen:
                return
            seen.add(url)
            image_urls.append(url)

        opus = major.get("opus", {}) if isinstance(major.get("opus"), dict) else {}
        for pic in opus.get("pics", []) if isinstance(opus.get("pics"), list) else []:
            if not isinstance(pic, dict):
                continue
            append_candidate(pic.get("url") or pic.get("orig_url") or pic.get("img_src"))

        draw = major.get("draw", {}) if isinstance(major.get("draw"), dict) else {}
        for pic in draw.get("items", []) if isinstance(draw.get("items"), list) else []:
            if not isinstance(pic, dict):
                continue
            append_candidate(pic.get("src") or pic.get("url") or pic.get("img_src"))

        return image_urls

    def _parse_video_post(self, item: Dict[str, Any]) -> Optional[BilibiliVideoPost]:
        bvid = item.get("bvid") or self._find_first_value(item, ("bvid",))
        aid = item.get("aid") or self._find_first_value(item, ("aid",))
        video_id = bvid or aid
        if video_id is None:
            return None

        title_value = item.get("title") or self._find_first_value(item, ("title",))
        title = str(title_value).strip() if title_value is not None else "发布了新视频"

        url_value = item.get("url") or item.get("link")
        url = _normalize_url(str(url_value).strip() if url_value is not None else "")
        if not url and bvid:
            url = f"https://www.bilibili.com/video/{bvid}"
        if not url and aid:
            url = f"https://www.bilibili.com/video/av{aid}"

        cover_value = item.get("pic") or self._find_first_value(item, ("pic", "cover"))
        cover_url = _normalize_url(str(cover_value).strip() if cover_value is not None else "")

        return BilibiliVideoPost(
            id=str(video_id),
            title=title or "发布了新视频",
            url=url or "https://www.bilibili.com",
            cover_url=cover_url,
        )

    def _extract_nested_text(self, value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, dict):
            for key in ("text", "title", "desc", "content", "summary"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
                nested = self._extract_nested_text(candidate)
                if nested:
                    return nested
        if isinstance(value, list):
            for item in value:
                nested = self._extract_nested_text(item)
                if nested:
                    return nested
        return ""

    def _find_first_value(self, value: Any, candidate_keys: Sequence[str]) -> Optional[Any]:
        if isinstance(value, dict):
            for key in candidate_keys:
                if key in value and value[key] not in (None, ""):
                    return value[key]
            for nested in value.values():
                result = self._find_first_value(nested, candidate_keys)
                if result not in (None, ""):
                    return result
        if isinstance(value, list):
            for item in value:
                result = self._find_first_value(item, candidate_keys)
                if result not in (None, ""):
                    return result
        return None


class BilibiliMonitorService:
    def __init__(self, gateway: BilibiliGateway) -> None:
        self._gateway = gateway

    async def poll(
        self,
        config: BilibiliPushConfig,
        state: Optional[Dict[str, Any]],
    ) -> tuple[Dict[str, Any], List[BilibiliNotification]]:
        new_state = deepcopy(state or {})
        uid_state_map = new_state.setdefault("uids", {})
        notifications: List[BilibiliNotification] = []

        for uid in config.target_uids:
            previous_uid_state = uid_state_map.get(uid, {})
            try:
                current_uid_state, uid_notifications = await self._poll_uid(
                    uid=uid,
                    config=config,
                    previous_state=previous_uid_state,
                )
            except Exception:
                logger.exception("轮询 B 站 UID %s 失败", uid)
                continue

            uid_state_map[uid] = current_uid_state
            notifications.extend(uid_notifications)

        return new_state, notifications

    async def _poll_uid(
        self,
        uid: str,
        config: BilibiliPushConfig,
        previous_state: Dict[str, Any],
    ) -> tuple[Dict[str, Any], List[BilibiliNotification]]:
        uid_state = deepcopy(previous_state or {})
        author_name = uid_state.get("author_name") or await self._gateway.get_user_name(uid)
        uid_state["author_name"] = author_name

        notifications: List[BilibiliNotification] = []

        if config.push_dynamic:
            latest_dynamic_id = str(uid_state.get("last_dynamic_id") or "").strip() or None
            dynamics = await self._gateway.get_recent_dynamics(uid, stop_at_id=latest_dynamic_id)
            if dynamics:
                if latest_dynamic_id is not None:
                    for post in reversed(dynamics):
                        notifications.append(
                            BilibiliNotification(
                                kind="dynamic",
                                uid=uid,
                                author_name=author_name,
                                title="",
                                url=post.url,
                                text=post.text,
                                rich_nodes=post.rich_nodes,
                                image_urls=post.image_urls,
                            )
                        )
                uid_state["last_dynamic_id"] = dynamics[0].id

        if config.push_video:
            latest_video_id = str(uid_state.get("last_video_id") or "").strip() or None
            videos = await self._gateway.get_recent_videos(uid, stop_at_id=latest_video_id)
            if videos:
                if latest_video_id is not None:
                    for post in reversed(videos):
                        notifications.append(
                            BilibiliNotification(
                                kind="video",
                                uid=uid,
                                author_name=author_name,
                                title=post.title,
                                url=post.url,
                                cover_url=post.cover_url,
                            )
                        )
                uid_state["last_video_id"] = videos[0].id

        if config.push_live:
            live_status = await self._gateway.get_live_status(uid)
            if live_status is not None:
                previous_live_active = uid_state.get("last_live_active")
                if previous_live_active is None:
                    uid_state["last_live_active"] = live_status.is_live
                    uid_state["last_live_room_id"] = live_status.room_id
                else:
                    if live_status.is_live and not bool(previous_live_active):
                        notifications.append(
                            BilibiliNotification(
                                kind="live",
                                uid=uid,
                                author_name=author_name,
                                title=live_status.title or "直播已开始",
                                url=live_status.url,
                                cover_url=live_status.cover_url,
                            )
                        )
                    uid_state["last_live_active"] = live_status.is_live
                    uid_state["last_live_room_id"] = live_status.room_id

        return uid_state, notifications


def _normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    return text
