import logging
import json
import time
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
DEFAULT_POLL_INTERVAL_SECONDS = 300
MIN_POLL_INTERVAL_SECONDS = 60
DEFAULT_TASK_GAP_SECONDS = 20.0
COMMENT_RESOURCE_LIMIT_PER_KIND = 2
COMMENT_RECENT_IDS_LIMIT = 20
CONTENT_RECENT_IDS_LIMIT = 20
RECENT_NOTIFICATION_WINDOW_SECONDS = 5 * 60
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
    task_gap_seconds: float
    group_whitelist: List[str]
    target_uids: List[str]
    push_dynamic: bool
    push_video: bool
    push_live: bool
    push_comment: bool
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
    title: str = ""
    cover_url: str = ""
    created_at: int = 0
    comment_oid: int = 0
    comment_type: int = 0
    is_live_room_dynamic: bool = False
    is_video_dynamic: bool = False


@dataclass(frozen=True)
class BilibiliVideoPost:
    id: str
    title: str
    url: str
    cover_url: str = ""
    created_at: int = 0
    comment_oid: int = 0


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
    comment_created_at: int = 0
    comment_resource_owner_name: str = ""
    comment_resource_kind: str = ""
    comment_resource_title: str = ""
    comment_action_text: str = ""


@dataclass(frozen=True)
class BilibiliCommentResource:
    key: str
    owner_uid: str
    owner_name: str
    resource_kind: str
    oid: int
    type_value: int
    title: str
    url: str


@dataclass(frozen=True)
class BilibiliCommentPost:
    id: str
    author_uid: str
    author_name: str
    text: str
    created_at: int
    is_reply: bool
    image_urls: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class BilibiliUidSnapshot:
    uid: str
    author_name: str
    dynamics: List[BilibiliDynamicPost] = field(default_factory=list)
    live_status: Optional[BilibiliLiveStatus] = None
    comment_resources: List[BilibiliCommentResource] = field(default_factory=list)
    comment_posts: Dict[str, List[BilibiliCommentPost]] = field(default_factory=dict)


@dataclass(frozen=True)
class BilibiliPlannedNotification:
    notification: BilibiliNotification
    uid_state: Dict[str, Any]


@dataclass(frozen=True)
class BilibiliUidDeliveryPlan:
    deliveries: List[BilibiliPlannedNotification] = field(default_factory=list)
    final_state: Dict[str, Any] = field(default_factory=dict)


def build_bilibili_push_config(raw_config: Optional[Dict[str, Any]]) -> BilibiliPushConfig:
    source = raw_config or {}
    poll_interval = _safe_parse_int(
        source.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS),
        DEFAULT_POLL_INTERVAL_SECONDS,
    )
    task_gap_seconds = _safe_parse_float(
        source.get("task_gap_seconds", DEFAULT_TASK_GAP_SECONDS),
        DEFAULT_TASK_GAP_SECONDS,
    )
    request_client = str(source.get("request_client", "aiohttp") or "aiohttp").strip().lower()
    if request_client not in {"aiohttp", "httpx", "curl_cffi"}:
        request_client = "aiohttp"

    return BilibiliPushConfig(
        enabled=bool(source.get("enabled", False)),
        poll_interval_seconds=max(MIN_POLL_INTERVAL_SECONDS, poll_interval),
        task_gap_seconds=max(0.0, task_gap_seconds),
        group_whitelist=_normalize_string_list(source.get("group_whitelist", [])),
        target_uids=_normalize_string_list(source.get("target_uids", DEFAULT_BILIBILI_TARGET_UIDS)),
        push_dynamic=bool(source.get("push_dynamic", True)),
        push_video=bool(source.get("push_video", True)),
        push_live=bool(source.get("push_live", True)),
        push_comment=bool(source.get("push_comment", False)),
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


def _safe_parse_int(raw_value: Any, default: int) -> int:
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


def _safe_parse_float(raw_value: Any, default: float) -> float:
    try:
        return float(raw_value)
    except (TypeError, ValueError):
        return default


def normalize_bilibili_uid(raw_value: Any) -> str:
    uid = str(raw_value or "").strip()
    if not uid or not uid.isdigit():
        raise ValueError("B站 UID 必须为纯数字字符串")
    return uid


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
        from bilibili_api import Credential, comment, select_client, user

        if not self._client_selected:
            select_client(self._request_client)
            self._client_selected = True
        return user, Credential, comment

    def _build_credential(self, credential_data: Dict[str, str]):
        if not credential_data.get("sessdata"):
            return None
        _, credential_cls, _ = self._load_modules()
        return credential_cls(**credential_data)

    def set_credential_data(self, credential_data: Optional[Dict[str, str]]) -> None:
        self._credential_data = _normalize_credential_data(credential_data or {})
        self._credential = self._build_credential(self._credential_data) if self._credential_data else None

    def set_request_client(self, request_client: str) -> None:
        normalized = str(request_client or "aiohttp").strip().lower()
        if normalized not in {"aiohttp", "httpx", "curl_cffi"}:
            normalized = "aiohttp"
        if normalized == self._request_client:
            return
        self._request_client = normalized
        self._client_selected = False

    def clear_credential(self) -> None:
        self._credential_data = {}
        self._credential = None

    def get_credential_data(self) -> Dict[str, str]:
        return dict(self._credential_data)

    def has_credential(self) -> bool:
        return bool(self._credential and self._credential.has_sessdata())

    def _new_user(self, uid: str):
        user_module, _, _ = self._load_modules()
        normalized_uid = normalize_bilibili_uid(uid)
        kwargs: Dict[str, Any] = {"uid": int(normalized_uid)}
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
        max_items: Optional[int] = None,
    ) -> List[BilibiliDynamicPost]:
        posts, _ = await self.get_recent_dynamics_with_status(
            uid=uid,
            stop_at_id=stop_at_id,
            max_items=max_items,
        )
        return posts

    async def get_recent_dynamics_with_status(
        self,
        uid: str,
        stop_at_id: Optional[str],
        max_items: Optional[int] = None,
    ) -> tuple[List[BilibiliDynamicPost], bool]:
        user_obj = self._new_user(uid)
        page = await user_obj.get_dynamics_new(offset="")
        collected: List[BilibiliDynamicPost] = []
        seen_ids = set()
        stop_found = stop_at_id is None
        items = self._extract_dynamic_items(page)
        for item in items:
            parsed = self._parse_dynamic_post(item)
            if parsed is None or parsed.id in seen_ids:
                continue
            if stop_at_id and parsed.id == stop_at_id:
                stop_found = True
                break
            if self._is_pinned_dynamic(item):
                continue
            seen_ids.add(parsed.id)
            collected.append(parsed)
            if max_items is not None and len(collected) >= max_items:
                break

        return collected, stop_found

    async def get_recent_videos(
        self,
        uid: str,
        stop_at_id: Optional[str],
        max_items: Optional[int] = None,
    ) -> List[BilibiliVideoPost]:
        posts, _ = await self.get_recent_videos_with_status(
            uid=uid,
            stop_at_id=stop_at_id,
            max_items=max_items,
        )
        return posts

    async def get_recent_videos_with_status(
        self,
        uid: str,
        stop_at_id: Optional[str],
        max_items: Optional[int] = None,
    ) -> tuple[List[BilibiliVideoPost], bool]:
        user_obj = self._new_user(uid)
        page = await user_obj.get_videos(pn=1, ps=30)
        collected: List[BilibiliVideoPost] = []
        seen_ids = set()
        stop_found = stop_at_id is None
        items = self._extract_video_items(page)
        for item in items:
            parsed = self._parse_video_post(item)
            if parsed is None or parsed.id in seen_ids:
                continue
            if stop_at_id and parsed.id == stop_at_id:
                stop_found = True
                break
            seen_ids.add(parsed.id)
            collected.append(parsed)
            if max_items is not None and len(collected) >= max_items:
                break

        return collected, stop_found

    async def get_latest_dynamics(self, uid: str, limit: int) -> List[BilibiliDynamicPost]:
        return await self.get_recent_dynamics(uid, stop_at_id=None, max_items=max(1, limit))

    async def get_latest_videos(self, uid: str, limit: int) -> List[BilibiliVideoPost]:
        return await self.get_recent_videos(uid, stop_at_id=None, max_items=max(1, limit))

    async def get_raw_dynamics_page(self, uid: str, offset: str = "") -> Dict[str, Any]:
        user_obj = self._new_user(uid)
        page = await user_obj.get_dynamics_new(offset=offset)
        return page if isinstance(page, dict) else {"payload": page}

    async def get_raw_live_info(self, uid: str) -> Dict[str, Any]:
        user_obj = self._new_user(uid)
        info = await user_obj.get_live_info()
        return info if isinstance(info, dict) else {"payload": info}

    async def get_live_status(self, uid: str) -> Optional[BilibiliLiveStatus]:
        user_obj = self._new_user(uid)
        info = await user_obj.get_live_info()

        live_status_value = self._find_value_by_paths(
            info,
            (
                ("live_status",),
                ("liveStatus",),
                ("roomStatus",),
                ("room_info", "live_status"),
                ("room_info", "liveStatus"),
                ("room_info", "roomStatus"),
                ("live_room", "live_status"),
                ("live_room", "liveStatus"),
                ("live_room", "roomStatus"),
            ),
        )
        if live_status_value is None:
            return None

        room_id_value = self._find_value_by_paths(
            info,
            (
                ("roomid",),
                ("room_id",),
                ("roomId",),
                ("room_info", "room_id"),
                ("room_info", "roomid"),
                ("live_room", "room_id"),
                ("live_room", "roomid"),
            ),
        )
        title_value = self._find_value_by_paths(
            info,
            (
                ("room_info", "title"),
                ("live_room", "title"),
                ("room_data", "title"),
                ("title",),
                ("roomtitle",),
            ),
        )
        url_value = self._find_value_by_paths(
            info,
            (
                ("room_info", "url"),
                ("live_room", "url"),
                ("room_data", "url"),
                ("url",),
                ("link",),
            ),
        )
        cover_value = self._find_value_by_paths(
            info,
            (
                ("room_info", "cover"),
                ("room_info", "cover_from_user"),
                ("room_info", "user_cover"),
                ("live_room", "cover"),
                ("cover_from_user",),
                ("user_cover",),
                ("cover",),
                ("keyframe",),
            ),
        )

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

    async def get_live_status_by_uid(self, uid: str) -> Optional[BilibiliLiveStatus]:
        result = await self.get_live_status_by_uids([uid])
        return result.get(str(uid))

    async def get_live_status_by_uids(
        self,
        uids: Sequence[str],
    ) -> Dict[str, BilibiliLiveStatus]:
        normalized_uids = [normalize_bilibili_uid(uid) for uid in uids]
        if not normalized_uids:
            return {}

        self._load_modules()
        from bilibili_api.utils.network import Api

        params = {"uids[]": [int(uid) for uid in normalized_uids]}
        response = await Api(
            url="https://api.live.bilibili.com/room/v1/Room/get_status_info_by_uids",
            method="GET",
            verify=False,
            params={"uids[]": "list<int>: 主播uid列表"},
            comment="通过主播uid列表获取直播间状态信息",
            no_csrf=True,
        ).update_params(**params).result
        if not isinstance(response, dict):
            return {}

        result: Dict[str, BilibiliLiveStatus] = {}
        for uid in normalized_uids:
            raw_status = response.get(uid) or response.get(int(uid))
            if not isinstance(raw_status, dict):
                continue
            room_id = str(raw_status.get("room_id", "") or "").strip()
            url = (
                _normalize_url(str(raw_status.get("url", "") or "").strip())
                or (f"https://live.bilibili.com/{room_id}" if room_id else "https://live.bilibili.com")
            )
            result[uid] = BilibiliLiveStatus(
                is_live=int(raw_status.get("live_status", 0) or 0) == 1,
                title=str(raw_status.get("title", "") or "直播已开始"),
                room_id=room_id,
                url=url,
                cover_url=_normalize_url(
                    str(
                        raw_status.get("cover_from_user")
                        or raw_status.get("cover")
                        or raw_status.get("user_cover")
                        or ""
                    ).strip()
                ),
            )
        return result

    async def get_recent_comments(self, resource: BilibiliCommentResource) -> List[BilibiliCommentPost]:
        _, _, comment_module = self._load_modules()
        comment_type = comment_module.CommentResourceType(resource.type_value)
        page = await comment_module.get_comments_lazy(
            oid=resource.oid,
            type_=comment_type,
            order=comment_module.OrderType.TIME,
            credential=self._credential,
        )
        replies = page.get("replies")
        if not isinstance(replies, list):
            return []

        parsed: List[BilibiliCommentPost] = []
        seen_ids = set()

        def visit(reply_items: List[Dict[str, Any]]) -> None:
            for reply in reply_items:
                if not isinstance(reply, dict):
                    continue
                comment_post = self._parse_comment_post(reply)
                if comment_post and comment_post.id not in seen_ids:
                    seen_ids.add(comment_post.id)
                    parsed.append(comment_post)
                nested_replies = reply.get("replies")
                if isinstance(nested_replies, list) and nested_replies:
                    visit([item for item in nested_replies if isinstance(item, dict)])

        visit([item for item in replies if isinstance(item, dict)])
        parsed.sort(key=lambda item: (item.created_at, _safe_int(item.id)), reverse=True)
        return parsed

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

        url_value = self._extract_dynamic_url(item)
        url = _normalize_url(str(url_value).strip() if url_value is not None else "")
        if not url:
            url = f"https://t.bilibili.com/{dynamic_id}"

        text = plain_text or "发布了新动态"
        comment_oid = _safe_int(
            self._find_first_value(item.get("basic", {}), ("comment_id_str", "comment_id"))
        )
        comment_type = _safe_int(
            self._find_first_value(item.get("basic", {}), ("comment_type",))
        )
        created_at = _safe_int(
            self._find_value_by_paths(
                item,
                (
                    ("modules", "module_author", "pub_ts"),
                    ("modules", "module_author", "pub_ts_str"),
                    ("modules", "module_author", "ctime"),
                    ("modules", "module_author", "publish_ts"),
                    ("modules", "module_author", "publish_time"),
                    ("basic", "pub_ts"),
                    ("basic", "ctime"),
                    ("pub_ts",),
                    ("ctime",),
                ),
            )
        )
        major = self._get_module_dynamic(item).get("major")
        major = major if isinstance(major, dict) else {}
        archive = major.get("archive") if isinstance(major.get("archive"), dict) else {}
        is_video_dynamic = bool(archive) and any(
            archive.get(key) for key in ("bvid", "aid", "jump_url")
        )
        title = str(archive.get("title", "") or "").strip()
        cover_url = _normalize_url(str(archive.get("cover", "") or "").strip())

        return BilibiliDynamicPost(
            id=str(dynamic_id),
            text=text,
            url=url,
            rich_nodes=rich_nodes,
            image_urls=image_urls,
            title=title,
            cover_url=cover_url,
            created_at=created_at,
            comment_oid=comment_oid,
            comment_type=comment_type,
            is_live_room_dynamic=self._is_live_room_dynamic(item),
            is_video_dynamic=is_video_dynamic,
        )

    def _is_live_room_dynamic(self, item: Dict[str, Any]) -> bool:
        module_dynamic = self._get_module_dynamic(item)
        major = module_dynamic.get("major", {}) if isinstance(module_dynamic.get("major"), dict) else {}
        if not major:
            return False
        if isinstance(major.get("live_rcmd"), dict):
            return True
        live_block = major.get("live")
        if isinstance(live_block, dict) and live_block:
            return True
        return False

    def _extract_dynamic_rich_nodes(self, item: Dict[str, Any]) -> tuple[List[BilibiliRichTextNode], str]:
        nodes, primary_text = self._extract_primary_dynamic_rich_nodes(item)
        extra_parts = [
            part
            for part in (
                self._extract_dynamic_card_text(item),
                self._extract_dynamic_forward_text(item),
            )
            if part
        ]
        plain_text = "\n".join([part for part in [primary_text, *extra_parts] if part]).strip()

        if extra_parts:
            extra_text = "\n".join(extra_parts)
            if nodes:
                prefix = "\n" if primary_text else ""
                nodes = list(nodes) + [BilibiliRichTextNode(kind="text", text=f"{prefix}{extra_text}")]
            else:
                nodes = [BilibiliRichTextNode(kind="text", text=plain_text)]
        elif not nodes and plain_text:
            nodes = [BilibiliRichTextNode(kind="text", text=plain_text)]

        return nodes, plain_text

    def _extract_primary_dynamic_rich_nodes(self, item: Dict[str, Any]) -> tuple[List[BilibiliRichTextNode], str]:
        module_dynamic = self._get_module_dynamic(item)
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

    def _extract_dynamic_image_urls(self, item: Dict[str, Any], include_orig: bool = True) -> List[str]:
        module_dynamic = self._get_module_dynamic(item)
        major = module_dynamic.get("major", {}) if isinstance(module_dynamic.get("major"), dict) else {}
        additional = self._get_dynamic_additional(item)
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

        live_rcmd = self._extract_live_rcmd_payload(major.get("live_rcmd"))
        append_candidate(self._find_value_by_paths(live_rcmd, (("cover",), ("cover_url",), ("live_play_info", "cover"))))

        for block in (
            major.get("archive"),
            major.get("article"),
            major.get("common"),
            major.get("live"),
            additional.get("common"),
            additional.get("ugc"),
            additional.get("reserve"),
        ):
            if not isinstance(block, dict):
                continue
            append_candidate(
                self._find_value_by_paths(
                    block,
                    (
                        ("cover",),
                        ("cover_url",),
                        ("cover_src",),
                        ("image_url",),
                        ("image",),
                        ("head_text", "pic"),
                    ),
                )
            )

        if include_orig:
            orig = item.get("orig")
            if isinstance(orig, dict):
                for image_url in self._extract_dynamic_image_urls(orig, include_orig=False):
                    append_candidate(image_url)

        return image_urls

    def _extract_dynamic_url(self, item: Dict[str, Any]) -> str:
        url_value = (
            self._find_first_value(item.get("basic", {}), ("jump_url",))
            or item.get("jump_url")
            or item.get("url")
            or self._find_value_by_paths(
                item,
                (
                    ("modules", "module_dynamic", "major", "archive", "jump_url"),
                    ("modules", "module_dynamic", "major", "article", "jump_url"),
                    ("modules", "module_dynamic", "major", "live", "jump_url"),
                    ("modules", "module_dynamic", "additional", "reserve", "jump_url"),
                    ("modules", "module_dynamic", "additional", "common", "jump_url"),
                    ("modules", "module_dynamic", "additional", "ugc", "jump_url"),
                ),
            )
        )

        live_rcmd = self._extract_live_rcmd_payload(
            self._find_value_by_paths(item, (("modules", "module_dynamic", "major", "live_rcmd"),))
        )
        if not url_value:
            url_value = self._find_value_by_paths(
                live_rcmd,
                (
                    ("link",),
                    ("room_url",),
                    ("live_play_info", "link"),
                    ("live_play_info", "room_url"),
                ),
            )
        return _normalize_url(str(url_value or "").strip())

    def _extract_dynamic_card_text(self, item: Dict[str, Any]) -> str:
        module_dynamic = self._get_module_dynamic(item)
        major = module_dynamic.get("major", {}) if isinstance(module_dynamic.get("major"), dict) else {}
        additional = self._get_dynamic_additional(item)

        lines: List[str] = []
        live_rcmd = self._extract_live_rcmd_payload(major.get("live_rcmd"))

        self._append_unique_line(
            lines,
            self._find_value_by_paths(
                live_rcmd,
                (
                    ("title",),
                    ("room_name",),
                    ("live_play_info", "title"),
                    ("live_play_info", "room_name"),
                ),
            ),
        )

        for block in (
            major.get("archive"),
            major.get("article"),
            major.get("common"),
            major.get("live"),
            major.get("pgc"),
            additional.get("common"),
            additional.get("ugc"),
            additional.get("reserve"),
        ):
            if not isinstance(block, dict):
                continue
            self._append_unique_line(
                lines,
                self._find_value_by_paths(
                    block,
                    (
                        ("title",),
                        ("head_text", "text"),
                        ("subtitle",),
                    ),
                ),
            )
            self._append_unique_line(
                lines,
                self._find_value_by_paths(
                    block,
                    (
                        ("desc1", "text"),
                        ("desc_first",),
                    ),
                ),
            )
            self._append_unique_line(
                lines,
                self._find_value_by_paths(
                    block,
                    (
                        ("desc",),
                        ("sub_title",),
                        ("desc2", "text"),
                        ("desc_second",),
                        ("reserve_total", "text"),
                    ),
                ),
            )
            self._append_unique_line(
                lines,
                self._find_value_by_paths(
                    block,
                    (
                        ("desc3", "text"),
                        ("desc3",),
                    ),
                ),
            )

        return "\n".join(lines).strip()

    def _extract_dynamic_forward_text(self, item: Dict[str, Any]) -> str:
        orig = item.get("orig")
        if not isinstance(orig, dict):
            return ""

        modules = orig.get("modules", {}) if isinstance(orig.get("modules"), dict) else {}
        module_author = modules.get("module_author", {}) if isinstance(modules.get("module_author"), dict) else {}
        author_name = str(module_author.get("name", "") or "").strip()
        _, original_text = self._extract_primary_dynamic_rich_nodes(orig)
        original_card_text = self._extract_dynamic_card_text(orig)
        combined = "\n".join([part for part in (original_text, original_card_text) if part]).strip()
        if not combined:
            return ""
        if author_name:
            return f"转发自 {author_name}\n{combined}"
        return f"转发内容\n{combined}"

    @staticmethod
    def _append_unique_line(lines: List[str], raw_value: Any) -> None:
        text = str(raw_value or "").strip()
        if not text or text in lines:
            return
        lines.append(text)

    def _extract_live_rcmd_payload(self, raw_value: Any) -> Dict[str, Any]:
        if isinstance(raw_value, dict):
            content = raw_value.get("content")
            if isinstance(content, dict):
                return content
            if isinstance(content, str) and content.strip():
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    parsed = {}
                if isinstance(parsed, dict):
                    return parsed
            return raw_value
        if isinstance(raw_value, str) and raw_value.strip():
            try:
                parsed = json.loads(raw_value)
            except json.JSONDecodeError:
                return {}
            if isinstance(parsed, dict):
                return parsed
        return {}

    def _get_module_dynamic(self, item: Dict[str, Any]) -> Dict[str, Any]:
        modules = item.get("modules", {}) if isinstance(item.get("modules"), dict) else {}
        module_dynamic = modules.get("module_dynamic")
        return module_dynamic if isinstance(module_dynamic, dict) else {}

    def _get_dynamic_additional(self, item: Dict[str, Any]) -> Dict[str, Any]:
        module_dynamic = self._get_module_dynamic(item)
        additional = module_dynamic.get("additional")
        return additional if isinstance(additional, dict) else {}

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
        created_at = _safe_int(
            self._find_first_value(item, ("created", "created_at", "ctime", "pubdate"))
        )

        return BilibiliVideoPost(
            id=str(video_id),
            title=title or "发布了新视频",
            url=url or "https://www.bilibili.com",
            cover_url=cover_url,
            created_at=created_at,
            comment_oid=_safe_int(aid),
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

    def _parse_comment_post(self, reply: Dict[str, Any]) -> Optional[BilibiliCommentPost]:
        comment_id = reply.get("rpid_str") or reply.get("rpid")
        if comment_id is None:
            return None

        member = reply.get("member", {}) if isinstance(reply.get("member"), dict) else {}
        content = reply.get("content", {}) if isinstance(reply.get("content"), dict) else {}
        author_uid = str(member.get("mid", "") or "").strip()
        author_name = str(member.get("uname", "") or "").strip()
        text = str(content.get("message", "") or "").strip()
        image_urls = self._extract_comment_image_urls(content)
        if not author_uid or not author_name or (not text and not image_urls):
            return None

        parent_id = _safe_int(reply.get("parent"))
        return BilibiliCommentPost(
            id=str(comment_id),
            author_uid=author_uid,
            author_name=author_name,
            text=text,
            created_at=_safe_int(reply.get("ctime")),
            is_reply=parent_id > 0,
            image_urls=image_urls,
        )

    def _extract_comment_image_urls(self, content: Dict[str, Any]) -> List[str]:
        if not isinstance(content, dict):
            return []

        image_urls: List[str] = []
        seen = set()

        def append_candidate(raw_value: Any) -> None:
            url = _normalize_url(str(raw_value or "").strip())
            if not url or url in seen:
                return
            seen.add(url)
            image_urls.append(url)

        pictures = content.get("pictures")
        if isinstance(pictures, list):
            for picture in pictures:
                if not isinstance(picture, dict):
                    continue
                for key in ("img_src", "img_url", "url", "src"):
                    append_candidate(picture.get(key))

        emote = content.get("emote")
        if isinstance(emote, dict):
            for raw_item in emote.values():
                if not isinstance(raw_item, dict):
                    continue
                for key in ("url", "icon_url", "emote_url"):
                    append_candidate(raw_item.get(key))

        return image_urls

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

    def _find_value_by_paths(
        self,
        value: Any,
        candidate_paths: Sequence[Sequence[str]],
    ) -> Optional[Any]:
        for path in candidate_paths:
            current = value
            matched = True
            for key in path:
                if not isinstance(current, dict) or key not in current:
                    matched = False
                    break
                current = current[key]
            if matched and current not in (None, ""):
                return current
        return None


class BilibiliMonitorService:
    def __init__(self, gateway: BilibiliGateway) -> None:
        self._gateway = gateway

    @staticmethod
    def _normalize_recent_ids(raw_value: Any) -> List[str]:
        if not isinstance(raw_value, list):
            return []
        normalized: List[str] = []
        seen = set()
        for item in raw_value:
            text = str(item or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            normalized.append(text)
        return normalized

    @staticmethod
    def _merge_recent_ids(current_ids: List[str], previous_ids: List[str]) -> List[str]:
        merged: List[str] = []
        seen = set()
        for raw_id in current_ids + previous_ids:
            text = str(raw_id or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
            if len(merged) >= CONTENT_RECENT_IDS_LIMIT:
                break
        return merged

    @staticmethod
    def _filter_recent_posts(posts: List[Any], cutoff_ts: int) -> List[Any]:
        return [
            post
            for post in posts
            if getattr(post, "created_at", 0) > 0
            and int(getattr(post, "created_at", 0)) >= cutoff_ts
        ]

    def _select_posts_for_delivery(
        self,
        posts: List[Any],
        last_seen_id: Optional[str],
        recent_ids: List[str],
        stop_found: bool,
        cutoff_ts: int,
    ) -> List[Any]:
        known_ids = {text for text in ([last_seen_id] + recent_ids) if text}
        candidate_posts = [post for post in posts if getattr(post, "id", "") not in known_ids]
        if not candidate_posts:
            return []
        if last_seen_id and stop_found:
            return candidate_posts
        return self._filter_recent_posts(candidate_posts, cutoff_ts)

    @staticmethod
    def _slice_posts_before_stop(
        posts: List[Any], stop_at_id: Optional[str]
    ) -> tuple[List[Any], bool]:
        if not stop_at_id:
            return list(posts), True

        collected: List[Any] = []
        for post in posts:
            post_id = str(getattr(post, "id", "") or "").strip()
            if post_id and post_id == stop_at_id:
                return collected, True
            collected.append(post)
        return collected, False

    def _record_dynamic_id(self, uid_state: Dict[str, Any], dyn_id: str) -> None:
        text = str(dyn_id or "").strip()
        if not text:
            return

        recent_ids = self._normalize_recent_ids(uid_state.get("recent_dynamic_ids", []))
        uid_state["last_dynamic_id"] = text
        uid_state["recent_dynamic_ids"] = self._merge_recent_ids([text], recent_ids)

    @staticmethod
    def _record_comment_id(resource_state: Dict[str, Any], comment_id: str) -> None:
        text = str(comment_id or "").strip()
        if not text:
            return

        current_ids = [
            text,
            *[
                str(item).strip()
                for item in resource_state.get("recent_comment_ids", [])
                if str(item).strip() and str(item).strip() != text
            ],
        ]
        resource_state["initialized"] = True
        resource_state["last_comment_id"] = text
        resource_state["recent_comment_ids"] = current_ids[:COMMENT_RECENT_IDS_LIMIT]

    async def fetch_uid_snapshot(
        self,
        config: BilibiliPushConfig,
        uid: str,
        previous_state: Optional[Dict[str, Any]] = None,
    ) -> BilibiliUidSnapshot:
        previous_uid_state = previous_state if isinstance(previous_state, dict) else {}
        author_name = previous_uid_state.get("author_name") or await self._gateway.get_user_name(uid)

        dynamics: List[BilibiliDynamicPost] = []
        if config.push_dynamic or config.push_video or config.push_comment:
            dynamics, _ = await self._gateway.get_recent_dynamics_with_status(
                uid,
                stop_at_id=None,
                max_items=CONTENT_RECENT_IDS_LIMIT,
            )

        live_status: Optional[BilibiliLiveStatus] = None
        if config.push_live:
            live_status = await self._gateway.get_live_status_by_uid(uid)

        comment_resources: List[BilibiliCommentResource] = []
        comment_posts: Dict[str, List[BilibiliCommentPost]] = {}
        if config.push_comment:
            latest_dynamics = dynamics[:COMMENT_RESOURCE_LIMIT_PER_KIND]
            latest_videos = await self._gateway.get_latest_videos(
                uid, COMMENT_RESOURCE_LIMIT_PER_KIND
            )
            comment_resources = self._build_comment_resources(
                uid, author_name, latest_dynamics, latest_videos
            )
            watched_uids = {target_uid for target_uid in config.target_uids}
            for resource in comment_resources:
                comments = await self._gateway.get_recent_comments(resource)
                comment_posts[resource.key] = [
                    comment_post
                    for comment_post in comments
                    if comment_post.author_uid in watched_uids
                ]

        return BilibiliUidSnapshot(
            uid=uid,
            author_name=author_name,
            dynamics=dynamics,
            live_status=live_status,
            comment_resources=comment_resources,
            comment_posts=comment_posts,
        )

    def plan_uid_deliveries(
        self,
        config: BilibiliPushConfig,
        previous_state: Optional[Dict[str, Any]],
        snapshot: BilibiliUidSnapshot,
    ) -> BilibiliUidDeliveryPlan:
        uid_state = deepcopy(previous_state or {})
        uid_state["author_name"] = snapshot.author_name
        deliveries: List[BilibiliPlannedNotification] = []
        recent_cutoff_ts = max(0, int(time.time()) - RECENT_NOTIFICATION_WINDOW_SECONDS)

        if config.push_dynamic or config.push_video:
            latest_dynamic_id = str(uid_state.get("last_dynamic_id") or "").strip() or None
            recent_dynamic_ids = self._normalize_recent_ids(
                uid_state.get("recent_dynamic_ids", [])
            )
            dynamic_window, dynamic_stop_found = self._slice_posts_before_stop(
                snapshot.dynamics, latest_dynamic_id
            )
            deliver_dynamics = self._select_posts_for_delivery(
                posts=dynamic_window,
                last_seen_id=latest_dynamic_id,
                recent_ids=recent_dynamic_ids,
                stop_found=dynamic_stop_found,
                cutoff_ts=recent_cutoff_ts,
            )
            progress_state = deepcopy(uid_state)
            for post in reversed(deliver_dynamics):
                self._record_dynamic_id(progress_state, post.id)
                if post.is_live_room_dynamic:
                    continue
                if post.is_video_dynamic:
                    if config.push_video:
                        deliveries.append(
                            BilibiliPlannedNotification(
                                notification=BilibiliNotification(
                                    kind="video",
                                    uid=snapshot.uid,
                                    author_name=snapshot.author_name,
                                    title=post.title or "发布了新视频",
                                    url=post.url,
                                    cover_url=post.cover_url
                                    or (post.image_urls[0] if post.image_urls else ""),
                                ),
                                uid_state=deepcopy(progress_state),
                            )
                        )
                    continue
                if config.push_dynamic:
                    deliveries.append(
                        BilibiliPlannedNotification(
                            notification=BilibiliNotification(
                                kind="dynamic",
                                uid=snapshot.uid,
                                author_name=snapshot.author_name,
                                title="",
                                url=post.url,
                                text=post.text,
                                rich_nodes=post.rich_nodes,
                                image_urls=post.image_urls,
                            ),
                            uid_state=deepcopy(progress_state),
                        )
                    )
            if dynamic_window:
                uid_state["last_dynamic_id"] = dynamic_window[0].id
                uid_state["recent_dynamic_ids"] = self._merge_recent_ids(
                    [post.id for post in dynamic_window],
                    recent_dynamic_ids,
                )

        if config.push_live and snapshot.live_status is not None:
            previous_live_active = uid_state.get("last_live_active")
            if previous_live_active is None:
                uid_state["last_live_active"] = snapshot.live_status.is_live
                uid_state["last_live_room_id"] = snapshot.live_status.room_id
            else:
                if snapshot.live_status.is_live and not bool(previous_live_active):
                    live_state = deepcopy(uid_state)
                    live_state["last_live_active"] = snapshot.live_status.is_live
                    live_state["last_live_room_id"] = snapshot.live_status.room_id
                    deliveries.append(
                        BilibiliPlannedNotification(
                            notification=BilibiliNotification(
                                kind="live",
                                uid=snapshot.uid,
                                author_name=snapshot.author_name,
                                title=snapshot.live_status.title or "直播已开始",
                                url=snapshot.live_status.url,
                                cover_url=snapshot.live_status.cover_url,
                            ),
                            uid_state=deepcopy(live_state),
                        )
                    )
                uid_state["last_live_active"] = snapshot.live_status.is_live
                uid_state["last_live_room_id"] = snapshot.live_status.room_id

        if config.push_comment:
            resource_state_map = deepcopy(uid_state.get("comment_resources", {}))
            active_keys = {resource.key for resource in snapshot.comment_resources}

            for resource in snapshot.comment_resources:
                state = resource_state_map.get(resource.key)
                filtered_comments = list(snapshot.comment_posts.get(resource.key, []))

                if not filtered_comments:
                    resource_state_map[resource.key] = self._build_comment_resource_state(
                        [],
                        state,
                    )
                    continue

                if not isinstance(state, dict) or not state.get("initialized"):
                    resource_state_map[resource.key] = self._build_comment_resource_state(
                        filtered_comments,
                        state,
                    )
                    continue

                known_ids = {
                    str(item).strip()
                    for item in state.get("recent_comment_ids", [])
                    if str(item).strip()
                }
                last_comment_id = str(state.get("last_comment_id", "") or "").strip()
                if last_comment_id:
                    known_ids.add(last_comment_id)

                new_comments = [
                    comment_post
                    for comment_post in filtered_comments
                    if comment_post.id not in known_ids
                ]
                comment_progress_state = deepcopy(uid_state)
                comment_progress_state["comment_resources"] = deepcopy(resource_state_map)
                resource_progress_state = deepcopy(
                    comment_progress_state["comment_resources"].get(resource.key, state)
                    or {}
                )
                if not isinstance(resource_progress_state, dict):
                    resource_progress_state = {}

                for comment_post in sorted(
                    new_comments, key=lambda item: (item.created_at, _safe_int(item.id))
                ):
                    self._record_comment_id(resource_progress_state, comment_post.id)
                    comment_progress_state["comment_resources"][resource.key] = deepcopy(
                        resource_progress_state
                    )
                    deliveries.append(
                        BilibiliPlannedNotification(
                            notification=self._build_comment_notification(
                                resource, comment_post
                            ),
                            uid_state=deepcopy(comment_progress_state),
                        )
                    )

                resource_state_map[resource.key] = self._build_comment_resource_state(
                    filtered_comments,
                    state,
                )

            uid_state["comment_resources"] = {
                key: value
                for key, value in resource_state_map.items()
                if key in active_keys
            }

        return BilibiliUidDeliveryPlan(
            deliveries=deliveries,
            final_state=uid_state,
        )

    async def poll(
        self,
        config: BilibiliPushConfig,
        state: Optional[Dict[str, Any]],
    ) -> tuple[Dict[str, Any], List[BilibiliNotification]]:
        new_state = deepcopy(state or {})
        notifications: List[BilibiliNotification] = []

        for uid in config.target_uids:
            new_state, uid_notifications = await self.poll_uid(
                config=config,
                state=new_state,
                uid=uid,
            )
            notifications.extend(uid_notifications)

        return new_state, notifications

    async def poll_uid(
        self,
        config: BilibiliPushConfig,
        state: Optional[Dict[str, Any]],
        uid: str,
    ) -> tuple[Dict[str, Any], List[BilibiliNotification]]:
        new_state = deepcopy(state or {})
        uid_state_map = new_state.setdefault("uids", {})
        previous_uid_state = uid_state_map.get(uid, {})
        try:
            snapshot = await self.fetch_uid_snapshot(
                config=config,
                uid=uid,
                previous_state=previous_uid_state,
            )
            plan = self.plan_uid_deliveries(
                config=config,
                previous_state=previous_uid_state,
                snapshot=snapshot,
            )
        except Exception:
            logger.exception("轮询 B 站 UID %s 失败", uid)
            return new_state, []

        uid_state_map[uid] = plan.final_state
        return new_state, [delivery.notification for delivery in plan.deliveries]

    def _build_comment_resources(
        self,
        owner_uid: str,
        owner_name: str,
        dynamics: List[BilibiliDynamicPost],
        videos: List[BilibiliVideoPost],
    ) -> List[BilibiliCommentResource]:
        resources: List[BilibiliCommentResource] = []
        seen_keys = set()

        def append_resource(resource: BilibiliCommentResource) -> None:
            if resource.key in seen_keys:
                return
            seen_keys.add(resource.key)
            resources.append(resource)

        for post in dynamics:
            if post.is_video_dynamic:
                continue
            if post.comment_oid <= 0 or post.comment_type <= 0:
                continue
            append_resource(
                BilibiliCommentResource(
                    key=f"dynamic:{post.comment_type}:{post.comment_oid}",
                    owner_uid=owner_uid,
                    owner_name=owner_name,
                    resource_kind="dynamic",
                    oid=post.comment_oid,
                    type_value=post.comment_type,
                    title=_trim_text(post.text or "动态", 80),
                    url=post.url,
                )
            )

        for post in videos:
            if post.comment_oid <= 0:
                continue
            append_resource(
                BilibiliCommentResource(
                    key=f"video:{post.comment_oid}",
                    owner_uid=owner_uid,
                    owner_name=owner_name,
                    resource_kind="video",
                    oid=post.comment_oid,
                    type_value=1,
                    title=_trim_text(post.title or "视频", 80),
                    url=post.url,
                )
            )

        return resources

    def _build_comment_resource_state(
        self,
        comments: List[BilibiliCommentPost],
        previous_state: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        previous = previous_state if isinstance(previous_state, dict) else {}
        current_ids = [comment_post.id for comment_post in comments if comment_post.id]
        merged_ids: List[str] = []
        seen = set()
        for comment_id in current_ids + list(previous.get("recent_comment_ids", [])):
            text = str(comment_id).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged_ids.append(text)
            if len(merged_ids) >= COMMENT_RECENT_IDS_LIMIT:
                break

        last_comment_id = current_ids[0] if current_ids else str(previous.get("last_comment_id", "") or "").strip()
        return {
            "initialized": True,
            "last_comment_id": last_comment_id,
            "recent_comment_ids": merged_ids,
        }

    def _build_comment_notification(
        self,
        resource: BilibiliCommentResource,
        comment_post: BilibiliCommentPost,
    ) -> BilibiliNotification:
        resource_text = "动态" if resource.resource_kind == "dynamic" else "视频"
        action_text = "回复了评论" if comment_post.is_reply else "发表了评论"
        return BilibiliNotification(
            kind="comment",
            uid=comment_post.author_uid,
            author_name=comment_post.author_name,
            title="",
            url=resource.url,
            text=comment_post.text,
            image_urls=list(comment_post.image_urls),
            comment_created_at=comment_post.created_at,
            comment_resource_owner_name=resource.owner_name,
            comment_resource_kind=resource_text,
            comment_resource_title=resource.title,
            comment_action_text=action_text,
        )


def _normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        return f"https:{text}"
    return text


def _trim_text(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
