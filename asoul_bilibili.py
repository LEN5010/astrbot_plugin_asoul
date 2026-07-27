import asyncio
import logging
import json
import time
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

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
COMMENT_RESOURCE_LIMIT_PER_KIND = 3
COMMENT_FETCH_PAGE_LIMIT = 5
COMMENT_SUB_COMMENT_PAGE_SIZE = 20
COMMENT_POLL_INTERVAL_SECONDS = 180
COMMENT_RESOURCE_REFRESH_INTERVAL_SECONDS = 600
COMMENT_REQUEST_INTERVAL_SECONDS = 2.0
MIN_COMMENT_REQUEST_INTERVAL_SECONDS = 0.5
MAX_COMMENT_REQUEST_INTERVAL_SECONDS = 60.0
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
KV_BILIBILI_PROFILE_CACHE = "bilibili_profile_cache"

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
    render_bilibili_cards: bool = True
    comment_target_uids: List[str] = field(default_factory=list)
    comment_request_interval_seconds: float = COMMENT_REQUEST_INTERVAL_SECONDS


@dataclass(frozen=True)
class BilibiliRichTextNode:
    kind: str
    text: str = ""
    image_url: str = ""
    url: str = ""


@dataclass(frozen=True)
class BilibiliAuthorCardProfile:
    uid: str = ""
    name: str = ""
    avatar_url: str = ""
    pendant_url: str = ""
    total_likes: Optional[int] = None
    following: Optional[int] = None
    follower: Optional[int] = None
    fetched_at: int = 0


@dataclass(frozen=True)
class BilibiliEngagementStats:
    like_count: int = 0
    comment_count: int = 0
    forward_count: int = 0


@dataclass(frozen=True)
class BilibiliAdditionalCard:
    kind: str = ""
    title: str = ""
    subtitle: str = ""
    status: str = ""
    badge: str = ""
    cover_url: str = ""
    url: str = ""


@dataclass(frozen=True)
class BilibiliForwardedContent:
    author_name: str = ""
    avatar_url: str = ""
    text: str = ""
    rich_nodes: List[BilibiliRichTextNode] = field(default_factory=list)
    image_urls: List[str] = field(default_factory=list)
    title: str = ""


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
    author: BilibiliAuthorCardProfile = field(default_factory=BilibiliAuthorCardProfile)
    stats: BilibiliEngagementStats = field(default_factory=BilibiliEngagementStats)
    additional_card: BilibiliAdditionalCard = field(default_factory=BilibiliAdditionalCard)
    forwarded: Optional[BilibiliForwardedContent] = None
    is_pinned_dynamic: bool = False
    is_live_room_dynamic: bool = False
    is_video_dynamic: bool = False
    video_bvid: str = ""


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
    started_at: int = 0
    stats: BilibiliEngagementStats = field(default_factory=BilibiliEngagementStats)


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
    content_id: str = ""
    published_at: int = 0
    author_profile: BilibiliAuthorCardProfile = field(
        default_factory=BilibiliAuthorCardProfile
    )
    stats: BilibiliEngagementStats = field(default_factory=BilibiliEngagementStats)
    additional_card: BilibiliAdditionalCard = field(default_factory=BilibiliAdditionalCard)
    forwarded: Optional[BilibiliForwardedContent] = None
    video_bvid: str = ""
    stats_are_fallback: bool = False


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
    root_id: str = ""
    parent_id: str = ""
    image_urls: List[str] = field(default_factory=list)
    reply_count: int = 0


class BilibiliCommentPayloadError(ValueError):
    """The comments endpoint returned data that cannot advance safely."""


@dataclass(frozen=True)
class BilibiliRootReplyState:
    root_rpid: str
    reply_count: int = 0
    embedded_reply_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BilibiliRootCommentPage:
    posts: List[BilibiliCommentPost] = field(default_factory=list)
    next_offset: str = ""
    root_states: List[BilibiliRootReplyState] = field(default_factory=list)


@dataclass(frozen=True)
class BilibiliReplyCommentPage:
    posts: List[BilibiliCommentPost] = field(default_factory=list)
    next_page_index: int = 0


@dataclass(frozen=True)
class BilibiliUidSnapshot:
    uid: str
    author_name: str
    author_profile: BilibiliAuthorCardProfile = field(
        default_factory=BilibiliAuthorCardProfile
    )
    dynamics: List[BilibiliDynamicPost] = field(default_factory=list)
    live_status: Optional[BilibiliLiveStatus] = None


@dataclass(frozen=True)
class BilibiliPlannedNotification:
    notification: BilibiliNotification
    uid_state: Dict[str, Any]


@dataclass(frozen=True)
class BilibiliUidDeliveryPlan:
    deliveries: List[BilibiliPlannedNotification] = field(default_factory=list)
    final_state: Dict[str, Any] = field(default_factory=dict)


def merge_bilibili_author_profiles(
    cached: BilibiliAuthorCardProfile,
    content_author: BilibiliAuthorCardProfile,
    *,
    uid: str,
    name: str,
) -> BilibiliAuthorCardProfile:
    return BilibiliAuthorCardProfile(
        uid=content_author.uid or cached.uid or uid,
        name=content_author.name or cached.name or name,
        avatar_url=content_author.avatar_url or cached.avatar_url,
        pendant_url=content_author.pendant_url or cached.pendant_url,
        total_likes=cached.total_likes,
        following=cached.following,
        follower=cached.follower,
        fetched_at=cached.fetched_at,
    )


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
    target_uids = _normalize_string_list(
        source.get("target_uids", DEFAULT_BILIBILI_TARGET_UIDS)
    )
    comment_target_uids = _normalize_string_list(
        source.get("comment_target_uids", [])
    )
    if not comment_target_uids:
        comment_target_uids = list(target_uids)
    comment_request_interval = _safe_parse_float(
        source.get(
            "comment_request_interval_seconds",
            COMMENT_REQUEST_INTERVAL_SECONDS,
        ),
        COMMENT_REQUEST_INTERVAL_SECONDS,
    )

    return BilibiliPushConfig(
        enabled=bool(source.get("enabled", False)),
        poll_interval_seconds=max(MIN_POLL_INTERVAL_SECONDS, poll_interval),
        task_gap_seconds=max(0.0, task_gap_seconds),
        group_whitelist=_normalize_string_list(source.get("group_whitelist", [])),
        target_uids=target_uids,
        push_dynamic=bool(source.get("push_dynamic", True)),
        push_video=bool(source.get("push_video", True)),
        push_live=bool(source.get("push_live", True)),
        push_comment=bool(source.get("push_comment", False)),
        request_client=request_client,
        credential_data=_normalize_credential_data(source),
        render_bilibili_cards=bool(source.get("render_bilibili_cards", True)),
        comment_target_uids=comment_target_uids,
        comment_request_interval_seconds=min(
            MAX_COMMENT_REQUEST_INTERVAL_SECONDS,
            max(MIN_COMMENT_REQUEST_INTERVAL_SECONDS, comment_request_interval),
        ),
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
        comment_request_interval_seconds: float = COMMENT_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self._request_client = request_client
        self._client_selected = False
        self._credential_data = _normalize_credential_data(credential_data or {})
        self._credential = None
        self.set_comment_request_interval_seconds(comment_request_interval_seconds)
        self._comment_request_lock = asyncio.Lock()
        self._next_comment_request_at = 0.0
        if self._credential_data:
            self._credential = self._build_credential(self._credential_data)

    async def _execute_comment_request(
        self,
        request_factory: Callable[[], Awaitable[Any]],
    ) -> Any:
        async with self._comment_request_lock:
            wait_seconds = self._next_comment_request_at - time.monotonic()
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            try:
                return await request_factory()
            finally:
                self._next_comment_request_at = (
                    time.monotonic() + self._comment_request_interval_seconds
                )

    def _load_modules(self):
        from bilibili_api import Credential, comment, select_client, user

        if not self._client_selected:
            select_client(self._request_client)
            self._client_selected = True
        return user, Credential, comment

    def _load_video_module(self):
        from bilibili_api import select_client, video

        if not self._client_selected:
            select_client(self._request_client)
            self._client_selected = True
        return video

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

    def set_comment_request_interval_seconds(self, value: float) -> None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = COMMENT_REQUEST_INTERVAL_SECONDS
        self._comment_request_interval_seconds = min(
            MAX_COMMENT_REQUEST_INTERVAL_SECONDS,
            max(MIN_COMMENT_REQUEST_INTERVAL_SECONDS, parsed),
        )

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

    async def get_user_card_profile(self, uid: str) -> BilibiliAuthorCardProfile:
        normalized_uid = normalize_bilibili_uid(uid)
        user_obj = self._new_user(normalized_uid)
        responses = await asyncio.gather(
            user_obj.get_user_info(),
            user_obj.get_relation_info(),
            user_obj.get_up_stat(),
            return_exceptions=True,
        )
        errors = [item for item in responses if isinstance(item, Exception)]
        if len(errors) == len(responses):
            raise errors[0]

        info = responses[0] if isinstance(responses[0], dict) else {}
        relation = responses[1] if isinstance(responses[1], dict) else {}
        up_stat = responses[2] if isinstance(responses[2], dict) else {}
        pendant = info.get("pendant") if isinstance(info.get("pendant"), dict) else {}
        name = next(
            (
                str(info.get(key) or "").strip()
                for key in ("name", "uname", "nickname")
                if str(info.get(key) or "").strip()
            ),
            normalized_uid,
        )
        return BilibiliAuthorCardProfile(
            uid=normalized_uid,
            name=name,
            avatar_url=_normalize_url(info.get("face", "")),
            pendant_url=_normalize_url(
                pendant.get("image") or pendant.get("image_enhance") or ""
            ),
            total_likes=_optional_non_negative_int(
                up_stat.get("likes", up_stat.get("archive", {}).get("view"))
                if isinstance(up_stat.get("archive"), dict)
                else up_stat.get("likes")
            ),
            following=_optional_non_negative_int(relation.get("following")),
            follower=_optional_non_negative_int(relation.get("follower")),
            fetched_at=int(time.time()),
        )

    async def get_video_engagement_stats(
        self, bvid: str
    ) -> BilibiliEngagementStats:
        normalized_bvid = str(bvid or "").strip()
        if not normalized_bvid:
            raise ValueError("BVID 不能为空")

        video_module = self._load_video_module()
        kwargs: Dict[str, Any] = {"bvid": normalized_bvid}
        if self._credential is not None:
            kwargs["credential"] = self._credential
        info = await video_module.Video(**kwargs).get_info()
        stat = info.get("stat") if isinstance(info, dict) else None
        if not isinstance(stat, dict) or not any(
            key in stat for key in ("like", "reply", "share")
        ):
            raise RuntimeError("B 站视频详情缺少 stat 字段")
        return BilibiliEngagementStats(
            like_count=max(0, _safe_int(stat.get("like"))),
            comment_count=max(0, _safe_int(stat.get("reply"))),
            forward_count=max(0, _safe_int(stat.get("share"))),
        )

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
                parsed = replace(parsed, is_pinned_dynamic=True)
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

    async def get_comment_resource_dynamics(
        self, uid: str, limit: int
    ) -> List[BilibiliDynamicPost]:
        return await self._execute_comment_request(
            lambda: self.get_recent_dynamics(
                uid,
                stop_at_id=None,
                max_items=max(1, limit),
            )
        )

    async def get_comment_resource_videos(
        self, uid: str, limit: int
    ) -> List[BilibiliVideoPost]:
        return await self._execute_comment_request(
            lambda: self.get_recent_videos(
                uid,
                stop_at_id=None,
                max_items=max(1, limit),
            )
        )

    async def get_comment_resource_owner_name(self, uid: str) -> str:
        return await self._execute_comment_request(lambda: self.get_user_name(uid))

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

    async def get_root_comment_page(
        self,
        resource: BilibiliCommentResource,
        offset: str = "",
    ) -> BilibiliRootCommentPage:
        _, _, comment_module = self._load_modules()
        comment_type = comment_module.CommentResourceType(resource.type_value)
        payload = await self._execute_comment_request(
            lambda: comment_module.get_comments_lazy(
                oid=resource.oid,
                type_=comment_type,
                offset=str(offset or ""),
                order=comment_module.OrderType.TIME,
                credential=self._credential,
            )
        )
        if not isinstance(payload, dict):
            raise BilibiliCommentPayloadError("root comment payload must be a dict")

        posts: List[BilibiliCommentPost] = []
        root_states: List[BilibiliRootReplyState] = []
        seen_ids: set[str] = set()

        def append_reply(
            raw_reply: Dict[str, Any],
            root_id: str = "",
            embedded_ids: Optional[List[str]] = None,
        ) -> None:
            post = self._parse_comment_post(raw_reply, root_id=root_id)
            if post is None:
                raise BilibiliCommentPayloadError("comment reply is missing rpid")
            if post.id in seen_ids:
                return
            seen_ids.add(post.id)
            posts.append(post)
            if embedded_ids is not None and post.is_reply:
                embedded_ids.append(post.id)
            nested = raw_reply.get("replies")
            if not isinstance(nested, list):
                return
            nested_root_id = post.root_id or post.id
            for raw_nested in nested:
                if isinstance(raw_nested, dict):
                    append_reply(raw_nested, nested_root_id, embedded_ids)

        replies = payload.get("replies")
        if replies is not None and not isinstance(replies, list):
            raise BilibiliCommentPayloadError("root replies must be a list or null")
        for raw_reply in replies or []:
            if not isinstance(raw_reply, dict):
                raise BilibiliCommentPayloadError("root reply must be a dict")
            root_id = str(raw_reply.get("rpid_str") or raw_reply.get("rpid") or "")
            embedded_ids: List[str] = []
            append_reply(raw_reply, embedded_ids=embedded_ids)
            root_states.append(
                BilibiliRootReplyState(
                    root_rpid=root_id,
                    reply_count=max(
                        _safe_int(raw_reply.get("rcount")),
                        len(embedded_ids),
                    ),
                    embedded_reply_ids=tuple(embedded_ids),
                )
            )
        return BilibiliRootCommentPage(
            posts=posts,
            next_offset=self._extract_comment_next_offset(payload),
            root_states=root_states,
        )

    async def get_reply_comment_page(
        self,
        resource: BilibiliCommentResource,
        root_id: str,
        page_index: int,
    ) -> BilibiliReplyCommentPage:
        _, _, comment_module = self._load_modules()
        root_rpid = _safe_int(root_id)
        normalized_page_index = max(1, int(page_index))
        if root_rpid <= 0:
            raise BilibiliCommentPayloadError("root rpid must be positive")
        comment_obj = comment_module.Comment(
            oid=resource.oid,
            type_=comment_module.CommentResourceType(resource.type_value),
            rpid=root_rpid,
            credential=self._credential,
        )
        payload = await self._execute_comment_request(
            lambda: comment_obj.get_sub_comments(
                page_index=normalized_page_index,
                page_size=COMMENT_SUB_COMMENT_PAGE_SIZE,
            )
        )
        if not isinstance(payload, dict):
            raise BilibiliCommentPayloadError("reply payload must be a dict")
        replies = payload.get("replies")
        if replies is not None and not isinstance(replies, list):
            raise BilibiliCommentPayloadError("nested replies must be a list or null")
        if not replies:
            return BilibiliReplyCommentPage()
        posts: List[BilibiliCommentPost] = []
        for raw_reply in replies:
            if not isinstance(raw_reply, dict):
                raise BilibiliCommentPayloadError("nested reply must be a dict")
            post = self._parse_comment_post(raw_reply, root_id=str(root_rpid))
            if post is None:
                raise BilibiliCommentPayloadError("nested reply is missing rpid")
            posts.append(post)
        next_page_index = (
            normalized_page_index + 1
            if len(replies) >= COMMENT_SUB_COMMENT_PAGE_SIZE
            else 0
        )
        return BilibiliReplyCommentPage(
            posts=posts,
            next_page_index=next_page_index,
        )

    async def get_recent_comments(
        self,
        resource: BilibiliCommentResource,
        stop_comment_ids: Optional[Sequence[str]] = None,
        stop_root_ids: Optional[Sequence[str]] = None,
        max_pages: int = COMMENT_FETCH_PAGE_LIMIT,
    ) -> List[BilibiliCommentPost]:
        _, _, comment_module = self._load_modules()
        comment_type = comment_module.CommentResourceType(resource.type_value)
        parsed: List[BilibiliCommentPost] = []
        seen_ids = set()
        stop_ids = {
            str(comment_id).strip()
            for comment_id in (stop_comment_ids or [])
            if str(comment_id).strip()
        }
        root_stop_ids = {
            str(comment_id).strip()
            for comment_id in (stop_root_ids or [])
            if str(comment_id).strip()
        }
        if not root_stop_ids:
            root_stop_ids = set(stop_ids)

        def visit(
            reply_items: List[Dict[str, Any]],
            page_comments: List[BilibiliCommentPost],
            root_id: str,
        ) -> None:
            for reply in reply_items:
                if not isinstance(reply, dict):
                    continue
                comment_post = self._parse_comment_post(reply, root_id=root_id)
                if comment_post:
                    page_comments.append(comment_post)
                nested_replies = reply.get("replies")
                if isinstance(nested_replies, list) and nested_replies:
                    visit(
                        [item for item in nested_replies if isinstance(item, dict)],
                        page_comments,
                        comment_post.root_id if comment_post else root_id,
                    )

        next_offset = ""
        for _ in range(max(1, int(max_pages or 0))):
            page = await self._execute_comment_request(
                lambda: comment_module.get_comments_lazy(
                    oid=resource.oid,
                    type_=comment_type,
                    offset=next_offset,
                    order=comment_module.OrderType.TIME,
                    credential=self._credential,
                )
            )
            if not isinstance(page, dict):
                break

            replies = page.get("replies")
            if not isinstance(replies, list) or not replies:
                break

            page_comments: List[BilibiliCommentPost] = []
            hit_known_root = False
            for reply in replies:
                if not isinstance(reply, dict):
                    continue
                root_comment = self._parse_comment_post(reply)
                if root_comment is None:
                    continue
                if root_comment.id in root_stop_ids:
                    hit_known_root = True
                elif hit_known_root:
                    continue
                page_comments.append(root_comment)
                nested_replies = reply.get("replies")
                if isinstance(nested_replies, list) and nested_replies:
                    visit(
                        [item for item in nested_replies if isinstance(item, dict)],
                        page_comments,
                        root_comment.root_id,
                    )
            page_comments.sort(
                key=lambda item: (item.created_at, _safe_int(item.id)),
                reverse=True,
            )

            for comment_post in page_comments:
                if comment_post.id in seen_ids:
                    continue
                if comment_post.is_reply and comment_post.id in stop_ids:
                    continue
                seen_ids.add(comment_post.id)
                parsed.append(comment_post)

            if hit_known_root:
                break

            new_offset = self._extract_comment_next_offset(page)
            if not new_offset or new_offset == next_offset:
                break
            next_offset = new_offset

        parsed.sort(key=lambda item: (item.created_at, _safe_int(item.id)), reverse=True)
        return parsed

    def _extract_comment_next_offset(self, page: Dict[str, Any]) -> str:
        cursor = page.get("cursor")
        if not isinstance(cursor, dict):
            return ""
        pagination_reply = cursor.get("pagination_reply")
        if not isinstance(pagination_reply, dict):
            return ""
        return str(pagination_reply.get("next_offset", "") or "").strip()

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
        forwarded = self._parse_forwarded_content(item)

        url_value = self._extract_dynamic_url(item)
        url = _normalize_url(str(url_value).strip() if url_value is not None else "")
        if not url:
            url = f"https://t.bilibili.com/{dynamic_id}"

        text = plain_text or ("" if forwarded is not None else "发布了新动态")
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
        is_live_room_dynamic = self._is_live_room_dynamic(item)
        if not is_video_dynamic and not is_live_room_dynamic:
            url = f"https://t.bilibili.com/{dynamic_id}"
        title = str(archive.get("title", "") or "").strip()
        cover_url = _normalize_url(str(archive.get("cover", "") or "").strip())
        video_bvid = str(archive.get("bvid", "") or "").strip()

        return BilibiliDynamicPost(
            id=str(dynamic_id),
            text=text,
            url=url,
            rich_nodes=rich_nodes,
            image_urls=image_urls,
            title=title,
            cover_url=cover_url,
            video_bvid=video_bvid,
            created_at=created_at,
            comment_oid=comment_oid,
            comment_type=comment_type,
            author=self._parse_dynamic_author(item),
            stats=self._parse_dynamic_stats(item),
            additional_card=self._parse_dynamic_additional_card(item),
            forwarded=forwarded,
            is_live_room_dynamic=is_live_room_dynamic,
            is_video_dynamic=is_video_dynamic,
        )

    def _parse_dynamic_author(self, item: Dict[str, Any]) -> BilibiliAuthorCardProfile:
        modules = item.get("modules", {}) if isinstance(item.get("modules"), dict) else {}
        author = modules.get("module_author", {})
        author = author if isinstance(author, dict) else {}
        pendant = author.get("pendant", {})
        pendant = pendant if isinstance(pendant, dict) else {}
        return BilibiliAuthorCardProfile(
            uid=str(author.get("mid", "") or author.get("uid", "") or "").strip(),
            name=str(author.get("name", "") or "").strip(),
            avatar_url=_normalize_url(str(author.get("face", "") or "")),
            pendant_url=_normalize_url(str(pendant.get("image", "") or "")),
        )

    def _parse_dynamic_stats(self, item: Dict[str, Any]) -> BilibiliEngagementStats:
        modules = item.get("modules", {}) if isinstance(item.get("modules"), dict) else {}
        stats = modules.get("module_stat", {})
        stats = stats if isinstance(stats, dict) else {}

        def count(name: str) -> int:
            value = stats.get(name, {})
            if isinstance(value, dict):
                value = value.get("count", value.get("num", 0))
            return max(0, _safe_int(value))

        return BilibiliEngagementStats(
            like_count=count("like"),
            comment_count=count("comment"),
            forward_count=count("forward"),
        )

    def _parse_dynamic_additional_card(
        self, item: Dict[str, Any]
    ) -> BilibiliAdditionalCard:
        module_dynamic = self._get_module_dynamic(item)
        major = module_dynamic.get("major", {})
        major = major if isinstance(major, dict) else {}
        additional = self._get_dynamic_additional(item)

        reserve = additional.get("reserve", {})
        if isinstance(reserve, dict) and reserve:
            subtitle_parts = [
                self._extract_nested_text(reserve.get("desc1", {})),
                self._extract_nested_text(reserve.get("desc2", {})),
            ]
            return BilibiliAdditionalCard(
                kind="reserve",
                title=str(reserve.get("title", "") or "").strip(),
                subtitle=" · ".join(part for part in subtitle_parts if part),
                status=self._extract_nested_text(reserve.get("button", {})),
                badge="预约",
                cover_url=_normalize_url(
                    str(
                        self._find_value_by_paths(
                            reserve,
                            (("cover",), ("cover_url",), ("head_text", "pic")),
                        )
                        or ""
                    )
                ),
                url=_normalize_url(str(reserve.get("jump_url", "") or "")),
            )

        archive = major.get("archive", {})
        if isinstance(archive, dict) and archive:
            return BilibiliAdditionalCard(
                kind="video",
                title=str(archive.get("title", "") or "").strip(),
                subtitle=str(archive.get("desc", "") or "").strip(),
                status=str(archive.get("badge", "") or "").strip(),
                badge="视频",
                cover_url=_normalize_url(str(archive.get("cover", "") or "")),
                url=_normalize_url(str(archive.get("jump_url", "") or "")),
            )

        live_payload = self._extract_live_rcmd_payload(major.get("live_rcmd"))
        if live_payload:
            return BilibiliAdditionalCard(
                kind="live",
                title=str(
                    self._find_value_by_paths(
                        live_payload,
                        (("title",), ("live_play_info", "title")),
                    )
                    or ""
                ).strip(),
                status="直播中",
                badge="直播",
                cover_url=_normalize_url(
                    str(
                        self._find_value_by_paths(
                            live_payload,
                            (("cover",), ("live_play_info", "cover")),
                        )
                        or ""
                    )
                ),
                url=_normalize_url(
                    str(
                        self._find_value_by_paths(
                            live_payload,
                            (("link",), ("live_play_info", "link")),
                        )
                        or ""
                    )
                ),
            )
        return BilibiliAdditionalCard()

    def _parse_forwarded_content(
        self, item: Dict[str, Any]
    ) -> Optional[BilibiliForwardedContent]:
        original = item.get("orig")
        if not isinstance(original, dict):
            return None
        author = self._parse_dynamic_author(original)
        rich_nodes, plain_text = self._extract_primary_dynamic_rich_nodes(original)
        module_dynamic = self._get_module_dynamic(original)
        major = module_dynamic.get("major", {})
        major = major if isinstance(major, dict) else {}
        title = str(
            self._find_value_by_paths(
                major,
                (("opus", "title"), ("archive", "title"), ("article", "title")),
            )
            or ""
        ).strip()
        return BilibiliForwardedContent(
            author_name=author.name,
            avatar_url=author.avatar_url,
            text=plain_text,
            rich_nodes=rich_nodes,
            image_urls=self._extract_dynamic_image_urls(original, include_orig=False)[:9],
            title=title,
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
                node_url = _normalize_url(raw_node.get("jump_url", ""))
                node_type = str(raw_node.get("type", "") or "").upper()
                if not node_url and node_type.endswith("_AT"):
                    mention_uid = str(raw_node.get("rid", "") or "").strip()
                    if mention_uid.isdigit():
                        node_url = f"https://space.bilibili.com/{mention_uid}"
                nodes.append(
                    BilibiliRichTextNode(
                        kind="link" if node_url else "text",
                        text=node_text,
                        url=node_url,
                    )
                )
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

        return image_urls[:9]

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

    def _parse_comment_post(
        self,
        reply: Dict[str, Any],
        root_id: str = "",
    ) -> Optional[BilibiliCommentPost]:
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
        raw_root_id = str(reply.get("root") or "").strip()
        comment_id_text = str(comment_id)
        parent_id_text = str(parent_id) if parent_id > 0 else ""
        if not parent_id_text and root_id and comment_id_text != str(root_id):
            parent_id_text = str(root_id)
        normalized_root_id = str(root_id or raw_root_id or "").strip()
        if not normalized_root_id or normalized_root_id == "0":
            normalized_root_id = parent_id_text or comment_id_text
        is_reply = bool(parent_id_text)
        if not is_reply:
            normalized_root_id = comment_id_text
        return BilibiliCommentPost(
            id=comment_id_text,
            author_uid=author_uid,
            author_name=author_name,
            text=text,
            created_at=_safe_int(reply.get("ctime")),
            is_reply=is_reply,
            root_id=normalized_root_id,
            parent_id=parent_id_text,
            image_urls=image_urls,
            reply_count=max(
                _safe_int(reply.get("rcount")),
                len(reply.get("replies"))
                if isinstance(reply.get("replies"), list)
                else 0,
            ),
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

    @staticmethod
    def _find_post_created_at(posts: List[Any], post_id: Optional[str]) -> int:
        target_id = str(post_id or "").strip()
        if not target_id:
            return 0
        for post in posts:
            if str(getattr(post, "id", "") or "").strip() != target_id:
                continue
            try:
                return max(0, int(getattr(post, "created_at", 0) or 0))
            except (TypeError, ValueError):
                return 0
        return 0

    @staticmethod
    def _select_cursor_post(
        posts: List[Any],
        min_created_at: int = 0,
    ) -> Optional[Any]:
        candidates = [
            post
            for post in posts
            if str(getattr(post, "id", "") or "").strip()
            and int(getattr(post, "created_at", 0) or 0) > 0
            and (
                min_created_at <= 0
                or int(getattr(post, "created_at", 0) or 0) > min_created_at
            )
        ]
        if not candidates:
            if min_created_at > 0:
                return None
            return posts[0] if posts else None
        return max(
            candidates,
            key=lambda post: (
                int(getattr(post, "created_at", 0) or 0),
                _safe_int(getattr(post, "id", "")),
            ),
        )

    def _select_posts_for_delivery(
        self,
        posts: List[Any],
        last_seen_id: Optional[str],
        recent_ids: List[str],
        stop_found: bool,
        cutoff_ts: int,
        last_seen_created_at: int = 0,
    ) -> List[Any]:
        known_ids = {text for text in ([last_seen_id] + recent_ids) if text}
        candidate_posts = []
        for post in posts:
            post_id = str(getattr(post, "id", "") or "").strip()
            if not post_id or post_id in known_ids:
                continue
            created_at = int(getattr(post, "created_at", 0) or 0)
            if getattr(post, "is_pinned_dynamic", False):
                if last_seen_created_at > 0:
                    if created_at <= last_seen_created_at:
                        continue
                elif created_at <= 0 or created_at < cutoff_ts:
                    continue
            candidate_posts.append(post)
        if not candidate_posts:
            return []
        if last_seen_id and stop_found:
            stale_candidate_posts = [
                post
                for post in candidate_posts
                if int(getattr(post, "created_at", 0) or 0) < cutoff_ts
            ]
            if stale_candidate_posts:
                recent_candidate_posts = self._filter_recent_posts(candidate_posts, cutoff_ts)
                if recent_candidate_posts:
                    return recent_candidate_posts
                return [
                    max(
                        candidate_posts,
                        key=lambda post: (
                            int(getattr(post, "created_at", 0) or 0),
                            _safe_int(getattr(post, "id", "")),
                        ),
                    )
                ]
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

    def _record_dynamic_id(
        self,
        uid_state: Dict[str, Any],
        dyn_id: str,
        created_at: int = 0,
    ) -> None:
        text = str(dyn_id or "").strip()
        if not text:
            return

        recent_ids = self._normalize_recent_ids(uid_state.get("recent_dynamic_ids", []))
        uid_state["last_dynamic_id"] = text
        uid_state["recent_dynamic_ids"] = self._merge_recent_ids([text], recent_ids)
        if created_at > 0:
            uid_state["last_dynamic_created_at"] = int(created_at)

    @staticmethod
    def _select_recent_comment_dynamics(
        dynamics: List[BilibiliDynamicPost],
    ) -> List[BilibiliDynamicPost]:
        candidates = [
            post
            for post in dynamics
            if not post.is_video_dynamic
            and post.comment_oid > 0
            and post.comment_type > 0
            and int(post.created_at or 0) > 0
        ]
        candidates.sort(
            key=lambda item: (int(item.created_at or 0), _safe_int(item.id)),
            reverse=True,
        )
        return candidates[:COMMENT_RESOURCE_LIMIT_PER_KIND]

    @staticmethod
    def _select_recent_comment_videos(
        videos: List[BilibiliVideoPost],
    ) -> List[BilibiliVideoPost]:
        candidates = [
            post
            for post in videos
            if post.comment_oid > 0 and int(post.created_at or 0) > 0
        ]
        candidates.sort(
            key=lambda item: (int(item.created_at or 0), _safe_int(item.id)),
            reverse=True,
        )
        return candidates[:COMMENT_RESOURCE_LIMIT_PER_KIND]

    async def fetch_uid_snapshot(
        self,
        config: BilibiliPushConfig,
        uid: str,
        previous_state: Optional[Dict[str, Any]] = None,
    ) -> BilibiliUidSnapshot:
        previous_uid_state = previous_state if isinstance(previous_state, dict) else {}
        author_name = previous_uid_state.get("author_name") or await self._gateway.get_user_name(uid)

        dynamics: List[BilibiliDynamicPost] = []
        if config.push_dynamic or config.push_video:
            dynamics, _ = await self._gateway.get_recent_dynamics_with_status(
                uid,
                stop_at_id=None,
                max_items=CONTENT_RECENT_IDS_LIMIT,
            )

        live_status: Optional[BilibiliLiveStatus] = None
        if config.push_live:
            live_status = await self._gateway.get_live_status_by_uid(uid)

        return BilibiliUidSnapshot(
            uid=uid,
            author_name=author_name,
            dynamics=dynamics,
            live_status=live_status,
        )

    async def discover_comment_resources(
        self,
        uid: str,
        author_name: str,
    ) -> List[BilibiliCommentResource]:
        dynamics = await self._gateway.get_comment_resource_dynamics(
            uid,
            CONTENT_RECENT_IDS_LIMIT,
        )
        videos = await self._gateway.get_comment_resource_videos(
            uid,
            CONTENT_RECENT_IDS_LIMIT,
        )
        return self._build_comment_resources(
            uid,
            author_name,
            self._select_recent_comment_dynamics(dynamics),
            self._select_recent_comment_videos(videos),
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
            sorted_dynamics = sorted(
                snapshot.dynamics,
                key=lambda post: (getattr(post, "created_at", 0) or 0, _safe_int(getattr(post, "id", ""))),
                reverse=True,
            )
            dynamic_window, dynamic_stop_found = self._slice_posts_before_stop(
                sorted_dynamics, latest_dynamic_id
            )
            last_dynamic_created_at = self._find_post_created_at(
                sorted_dynamics,
                latest_dynamic_id,
            )
            deliver_dynamics = self._select_posts_for_delivery(
                posts=dynamic_window,
                last_seen_id=latest_dynamic_id,
                recent_ids=recent_dynamic_ids,
                stop_found=dynamic_stop_found,
                cutoff_ts=recent_cutoff_ts,
                last_seen_created_at=last_dynamic_created_at,
            )
            progress_state = deepcopy(uid_state)
            for post in sorted(
                deliver_dynamics,
                key=lambda item: (item.created_at, _safe_int(item.id)),
            ):
                self._record_dynamic_id(progress_state, post.id, post.created_at)
                author_profile = merge_bilibili_author_profiles(
                    snapshot.author_profile,
                    post.author,
                    uid=snapshot.uid,
                    name=snapshot.author_name,
                )
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
                                    text=post.text,
                                    rich_nodes=post.rich_nodes,
                                    image_urls=post.image_urls,
                                    cover_url=post.cover_url
                                    or (post.image_urls[0] if post.image_urls else ""),
                                    content_id=post.id,
                                    video_bvid=post.video_bvid,
                                    published_at=post.created_at,
                                    author_profile=author_profile,
                                    stats=post.stats,
                                    additional_card=post.additional_card,
                                    forwarded=post.forwarded,
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
                                cover_url=post.cover_url,
                                content_id=post.id,
                                published_at=post.created_at,
                                author_profile=author_profile,
                                stats=post.stats,
                                additional_card=post.additional_card,
                                forwarded=post.forwarded,
                            ),
                            uid_state=deepcopy(progress_state),
                        )
                    )
            cursor_post = self._select_cursor_post(
                dynamic_window,
                last_dynamic_created_at,
            )
            if cursor_post is not None:
                uid_state["last_dynamic_id"] = cursor_post.id
                if getattr(cursor_post, "created_at", 0) > 0:
                    uid_state["last_dynamic_created_at"] = int(cursor_post.created_at)
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
                                content_id=snapshot.live_status.room_id,
                                published_at=snapshot.live_status.started_at,
                                author_profile=merge_bilibili_author_profiles(
                                    snapshot.author_profile,
                                    BilibiliAuthorCardProfile(),
                                    uid=snapshot.uid,
                                    name=snapshot.author_name,
                                ),
                                stats=snapshot.live_status.stats,
                                additional_card=BilibiliAdditionalCard(
                                    kind="live",
                                    title=snapshot.live_status.title or "直播已开始",
                                    status="直播中",
                                    badge="直播中",
                                    cover_url=snapshot.live_status.cover_url,
                                    url=snapshot.live_status.url,
                                ),
                            ),
                            uid_state=deepcopy(live_state),
                        )
                    )
                uid_state["last_live_active"] = snapshot.live_status.is_live
                uid_state["last_live_room_id"] = snapshot.live_status.room_id

        return BilibiliUidDeliveryPlan(
            deliveries=deliveries,
            final_state=uid_state,
        )

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

def _normalize_url(url: str) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = f"https:{text}"
    if not text.startswith(("http://", "https://")):
        return ""
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


def _optional_non_negative_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None
