import asyncio
import base64
import html
import io
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Sequence

from asoul_bilibili import BilibiliNotification, BilibiliRichTextNode


CARD_WIDTH_PX = 1200
CARD_RENDER_CACHE_TTL_SECONDS = 30 * 60
CARD_RENDER_MIN_BYTES = 1024
DISPLAY_TZ = timezone(timedelta(hours=8))
TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "bilibili_card.html"
BRAND_LOGO_PATH = Path(__file__).resolve().parent / "logo.png"
BRAND_NAME = "爱驼推送"


def safe_http_url(raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    if value.startswith("//"):
        value = f"https:{value}"
    if not value.startswith(("http://", "https://")):
        return ""
    return value


def format_card_number(raw_value: Optional[int]) -> str:
    if raw_value is None:
        return "--"
    try:
        value = max(0, int(raw_value))
    except (TypeError, ValueError):
        return "--"
    if value >= 100_000_000:
        return f"{_compact_decimal(value / 100_000_000)}亿"
    if value >= 10_000:
        return f"{_compact_decimal(value / 10_000)}万"
    return str(value)


def _compact_decimal(value: float) -> str:
    if value >= 100:
        digits = 1
    elif value >= 10:
        digits = 1
    else:
        digits = 2
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _escaped_text_with_breaks(value: Any) -> str:
    return html.escape(str(value or ""), quote=True).replace("\n", "<br>")


def render_rich_text_html(
    rich_nodes: Sequence[BilibiliRichTextNode],
    fallback_text: str,
) -> str:
    if not rich_nodes:
        return _escaped_text_with_breaks(fallback_text)
    parts: list[str] = []
    for node in rich_nodes:
        text = str(node.text or "")
        if node.kind == "emoji":
            image_url = safe_http_url(node.image_url)
            if image_url:
                parts.append(
                    '<img class="inline-emoji" src="{}" alt="{}">'.format(
                        html.escape(image_url, quote=True),
                        html.escape(text, quote=True),
                    )
                )
                continue
        if node.kind == "link":
            node_url = safe_http_url(node.url)
            if node_url:
                parts.append(
                    '<a href="{}">{}</a>'.format(
                        html.escape(node_url, quote=True),
                        _escaped_text_with_breaks(text),
                    )
                )
                continue
        parts.append(_escaped_text_with_breaks(text))
    return "".join(parts)


def build_qr_data_uri(url: str) -> str:
    safe_url = safe_http_url(url)
    if not safe_url:
        return ""
    import qrcode

    qr = qrcode.QRCode(version=None, box_size=8, border=2)
    qr.add_data(safe_url)
    qr.make(fit=True)
    image = qr.make_image(fill_color="#242424", back_color="white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


@lru_cache(maxsize=1)
def build_brand_logo_data_uri() -> str:
    try:
        payload = BRAND_LOGO_PATH.read_bytes()
    except OSError:
        return ""
    if not payload:
        return ""
    mime_type = "image/jpeg" if payload.startswith(b"\xff\xd8") else "image/png"
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _format_timestamp(timestamp: int, *, include_seconds: bool = False) -> str:
    if int(timestamp or 0) <= 0:
        return "--"
    pattern = "%Y-%m-%d %H:%M:%S" if include_seconds else "%Y-%m-%d %H:%M"
    return datetime.fromtimestamp(int(timestamp), DISPLAY_TZ).strftime(pattern)


def build_card_context(
    notification: BilibiliNotification,
    *,
    generated_at: Optional[int] = None,
) -> dict[str, Any]:
    generated_ts = int(generated_at or time.time())
    profile = notification.author_profile
    author_name = profile.name or notification.author_name or notification.uid
    profile_uid = profile.uid or notification.uid
    images = [
        url
        for url in (safe_http_url(item) for item in notification.image_urls)
        if url
    ][:9]
    cover_url = safe_http_url(notification.cover_url)
    if notification.kind in {"video", "live"} and cover_url and not images:
        images = [cover_url]

    additional = asdict(notification.additional_card)
    for key in ("kind", "title", "subtitle", "status", "badge"):
        additional[key] = html.escape(str(additional.get(key) or ""), quote=True)
    if not additional.get("badge"):
        additional["badge"] = additional.get("status", "")
    for key in ("cover_url", "url"):
        additional[key] = safe_http_url(additional.get(key))
    forwarded = None
    if notification.forwarded is not None:
        forwarded = {
            "author_name": html.escape(notification.forwarded.author_name, quote=True),
            "avatar_url": safe_http_url(notification.forwarded.avatar_url),
            "title": html.escape(notification.forwarded.title, quote=True),
            "body_html": render_rich_text_html(
                notification.forwarded.rich_nodes,
                notification.forwarded.text,
            ),
            "images": [
                url
                for url in (
                    safe_http_url(item)
                    for item in notification.forwarded.image_urls
                )
                if url
            ][:9],
        }
        forwarded_image_urls = set(forwarded["images"])
        images = [url for url in images if url not in forwarded_image_urls]

    kind_labels = {
        "dynamic": "动态",
        "video": "新视频",
        "live": "正在直播",
    }
    body_fallback = notification.text
    if not body_fallback and notification.kind == "dynamic":
        body_fallback = "发布了新动态"
    return {
        "card_width": CARD_WIDTH_PX,
        "brand_name": BRAND_NAME,
        "brand_logo_data_uri": build_brand_logo_data_uri(),
        "kind": notification.kind,
        "kind_label": kind_labels.get(notification.kind, "B站通知"),
        "title": html.escape(notification.title or "", quote=True),
        "body_html": render_rich_text_html(notification.rich_nodes, body_fallback),
        "published_at": _format_timestamp(notification.published_at),
        "generated_at": _format_timestamp(generated_ts, include_seconds=True),
        "url": safe_http_url(notification.url),
        "qr_data_uri": build_qr_data_uri(notification.url),
        "images": images,
        "image_count": len(images),
        "author": {
            "uid": html.escape(profile_uid, quote=True),
            "name": html.escape(author_name, quote=True),
            "avatar_url": safe_http_url(profile.avatar_url),
            "pendant_url": safe_http_url(profile.pendant_url),
            "likes": format_card_number(profile.total_likes),
            "following": format_card_number(profile.following),
            "follower": format_card_number(profile.follower),
        },
        "stats": {
            "like": format_card_number(notification.stats.like_count),
            "comment": format_card_number(notification.stats.comment_count),
            "forward": format_card_number(notification.stats.forward_count),
        },
        "stats_note": "数据暂未刷新" if notification.stats_are_fallback else "",
        "additional": additional if additional.get("kind") else None,
        "forwarded": forwarded,
    }


class BilibiliCardRenderer:
    def __init__(self, owner: Any, template_path: Path = TEMPLATE_PATH) -> None:
        self._owner = owner
        self._template_path = Path(template_path)
        self._cache: dict[str, tuple[float, str]] = {}
        self._render_locks: dict[str, asyncio.Lock] = {}
        self._created_paths: set[str] = set()

    @staticmethod
    def _cache_key(notification: BilibiliNotification) -> str:
        identity = notification.content_id or notification.url
        stats = notification.stats
        return ":".join(
            (
                notification.kind,
                notification.uid,
                identity,
                str(stats.like_count),
                str(stats.comment_count),
                str(stats.forward_count),
                "fallback" if notification.stats_are_fallback else "fresh",
            )
        )

    @staticmethod
    def _is_valid_output(path_value: str) -> bool:
        path = Path(str(path_value or ""))
        try:
            return path.is_file() and path.stat().st_size >= CARD_RENDER_MIN_BYTES
        except OSError:
            return False

    async def render(self, notification: BilibiliNotification) -> str:
        key = self._cache_key(notification)
        now = time.monotonic()
        cached = self._cache.get(key)
        if (
            cached is not None
            and now - cached[0] < CARD_RENDER_CACHE_TTL_SECONDS
            and self._is_valid_output(cached[1])
        ):
            return cached[1]

        lock = self._render_locks.setdefault(key, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            cached = self._cache.get(key)
            if (
                cached is not None
                and now - cached[0] < CARD_RENDER_CACHE_TTL_SECONDS
                and self._is_valid_output(cached[1])
            ):
                return cached[1]
            render_method = getattr(self._owner, "html_render", None)
            if not callable(render_method):
                raise RuntimeError("当前 AstrBot 运行环境不支持 html_render")
            template = self._template_path.read_text(encoding="utf-8")
            output_path = await render_method(
                template,
                build_card_context(notification),
                return_url=False,
                options={
                    "type": "png",
                    "full_page": True,
                    "omit_background": True,
                    "animations": "disabled",
                    "scale": "css",
                },
            )
            output_path = str(output_path or "")
            if not self._is_valid_output(output_path):
                raise RuntimeError("B 站卡片渲染输出不存在或文件过小")
            self._created_paths.add(output_path)
            self._cache[key] = (time.monotonic(), output_path)
            return output_path

    async def cleanup(self) -> None:
        paths = list(self._created_paths)
        self._created_paths.clear()
        self._cache.clear()
        self._render_locks.clear()
        for path_value in paths:
            try:
                Path(path_value).unlink(missing_ok=True)
            except OSError:
                pass
