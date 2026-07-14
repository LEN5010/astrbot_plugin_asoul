import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from bilibili_api import login_v2

from asoul_bilibili import (
    BilibiliAdditionalCard,
    BilibiliAuthorCardProfile,
    BilibiliNotification,
    merge_bilibili_author_profiles,
)
from asoul_bilibili_runtime import BilibiliRuntime
from asoul_core import DISPLAY_TZ

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


class BilibiliCommandService:
    def __init__(self, runtime: BilibiliRuntime) -> None:
        self._runtime = runtime

    async def ensure_private_bili_command(self, event: Any) -> Optional[str]:
        return await self._runtime.ensure_private_bili_command(event)

    async def build_dynamic_test_notification(self, uid: str) -> Optional[BilibiliNotification]:
        posts = await self._runtime.gateway.get_recent_dynamics(uid, stop_at_id=None)
        if not posts:
            return None

        post = posts[0]
        author_name = post.author.name or await self._runtime.gateway.get_user_name(uid)
        profile = await self._runtime.get_author_card_profile(
            uid,
            fallback=post.author
            if post.author.name or post.author.avatar_url
            else BilibiliAuthorCardProfile(uid=uid, name=author_name),
        )
        return BilibiliNotification(
            kind="dynamic",
            uid=uid,
            author_name=author_name,
            title="",
            url=post.url,
            text=post.text,
            rich_nodes=post.rich_nodes,
            image_urls=post.image_urls,
            cover_url=post.cover_url,
            content_id=post.id,
            published_at=post.created_at,
            author_profile=merge_bilibili_author_profiles(
                profile,
                post.author,
                uid=uid,
                name=author_name,
            ),
            stats=post.stats,
            additional_card=post.additional_card,
            forwarded=post.forwarded,
        )

    async def build_video_test_notification(self, uid: str) -> Optional[BilibiliNotification]:
        posts = await self._runtime.gateway.get_recent_videos(uid, stop_at_id=None)
        if not posts:
            return None

        post = posts[0]
        author_name = await self._runtime.gateway.get_user_name(uid)
        profile = await self._runtime.get_author_card_profile(
            uid,
            fallback=BilibiliAuthorCardProfile(uid=uid, name=author_name),
        )
        return BilibiliNotification(
            kind="video",
            uid=uid,
            author_name=author_name,
            title=post.title,
            url=post.url,
            cover_url=post.cover_url,
            content_id=post.id,
            video_bvid=post.id if str(post.id).upper().startswith("BV") else "",
            published_at=post.created_at,
            author_profile=profile,
            additional_card=BilibiliAdditionalCard(
                kind="video",
                title=post.title,
                status="已发布",
                badge="新视频",
                cover_url=post.cover_url,
                url=post.url,
            ),
        )

    async def build_live_test_notification(self, uid: str) -> tuple[str, Optional[BilibiliNotification]]:
        live_status = await self._runtime.gateway.get_live_status_by_uid(uid)
        if live_status is None:
            return f"UID {uid} 当前没有抓到直播间信息。", None

        author_name = await self._runtime.gateway.get_user_name(uid)
        if not live_status.is_live:
            return f"【B站直播状态】{author_name}\n当前未开播\n{live_status.url}", None

        profile = await self._runtime.get_author_card_profile(
            uid,
            fallback=BilibiliAuthorCardProfile(uid=uid, name=author_name),
        )
        return "", BilibiliNotification(
            kind="live",
            uid=uid,
            author_name=author_name,
            title=live_status.title,
            url=live_status.url,
            cover_url=live_status.cover_url,
            content_id=live_status.room_id,
            published_at=live_status.started_at,
            author_profile=profile,
            stats=live_status.stats,
            additional_card=BilibiliAdditionalCard(
                kind="live",
                title=live_status.title,
                status="直播中",
                badge="直播中",
                cover_url=live_status.cover_url,
                url=live_status.url,
            ),
        )

    async def build_comment_test_notifications(self, uid: str) -> list[BilibiliNotification]:
        owner_name = await self._runtime.gateway.get_user_name(uid)
        recent_dynamics = await self._runtime.gateway.get_recent_dynamics(uid, stop_at_id=None)
        recent_videos = await self._runtime.gateway.get_recent_videos(uid, stop_at_id=None)
        resources = self.build_comment_test_resources(
            uid,
            owner_name,
            recent_dynamics[:2],
            recent_videos[:2],
        )
        notifications: list[BilibiliNotification] = []
        watched_uids = {target_uid for target_uid in self._runtime.push_config.target_uids}
        for resource in resources:
            comments = await self._runtime.gateway.get_recent_comments(resource)
            filtered_comments = [
                comment_post
                for comment_post in comments
                if comment_post.author_uid in watched_uids
            ]
            for comment_post in sorted(filtered_comments, key=lambda item: (item.created_at, self.safe_int(item.id))):
                notifications.append(
                    self.build_comment_test_notification(resource, comment_post)
                )
        return notifications

    def build_comment_test_resources(
        self,
        owner_uid: str,
        owner_name: str,
        dynamics: Any,
        videos: Any,
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
                    title=self.trim_plain_text(post.text, 80),
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
                    title=self.trim_plain_text(post.title, 80),
                    url=post.url,
                )
            )

        return resources

    def build_comment_test_notification(self, resource: CommentTestResource, comment_post: Any) -> BilibiliNotification:
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

    async def dump_dynamic_payload(self, uid: str) -> Path:
        payload = await self._runtime.gateway.get_raw_dynamics_page(uid, offset="")
        return self.write_debug_payload_file(
            "dynamic",
            uid,
            {
                "uid": uid,
                "captured_at": datetime.now(DISPLAY_TZ).isoformat(),
                "payload": payload,
            },
        )

    async def dump_live_payload(self, uid: str) -> Path:
        payload = await self._runtime.gateway.get_raw_live_info(uid)
        return self.write_debug_payload_file(
            "live",
            uid,
            {
                "uid": uid,
                "captured_at": datetime.now(DISPLAY_TZ).isoformat(),
                "payload": payload,
            },
        )

    async def create_login_qrcode(self):
        await self._runtime.ensure_ready()
        QR_CODE_PATH.parent.mkdir(parents=True, exist_ok=True)
        qr_login = login_v2.QrCodeLogin(platform=login_v2.QrCodeLoginChannel.WEB)
        await qr_login.generate_qrcode()
        qr_login.get_qrcode_picture().to_file(str(QR_CODE_PATH))
        return qr_login, QR_CODE_PATH

    async def wait_for_login(self, qr_login: Any) -> str:
        while True:
            state = await qr_login.check_state()
            if state == login_v2.QrCodeLoginEvents.DONE:
                credential = qr_login.get_credential()
                await self._runtime.save_credential(credential.get_cookies())
                return "B 站登录成功，自动播报已恢复。"
            if state == login_v2.QrCodeLoginEvents.TIMEOUT:
                return "二维码已过期，请重新执行 /bili_login。"
            await asyncio.sleep(2)

    async def logout(self) -> None:
        await self._runtime.ensure_ready()
        await self._runtime.clear_credential()

    def write_debug_payload_file(self, kind: str, uid: str, payload: dict[str, Any]) -> Path:
        DEBUG_PAYLOAD_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(DISPLAY_TZ).strftime("%Y%m%d_%H%M%S")
        file_path = DEBUG_PAYLOAD_DIR / f"{kind}_{uid}_{timestamp}.json"
        file_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return file_path

    @staticmethod
    def normalize_command_uid(uid: str) -> str:
        normalized_uid = str(uid or "").strip()
        if not normalized_uid or not normalized_uid.isdigit():
            raise ValueError("B站 UID 必须为纯数字字符串")
        return normalized_uid

    @staticmethod
    def trim_plain_text(text: str, limit: int) -> str:
        compact = " ".join(str(text or "").split())
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 1)].rstrip() + "…"

    @staticmethod
    def safe_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
