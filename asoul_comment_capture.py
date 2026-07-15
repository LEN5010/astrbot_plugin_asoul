from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, Sequence

from asoul_bilibili import BilibiliGateway, BilibiliNotification
from asoul_comment_journal import CommentJournal, CommentScanTask

COMMENT_PRIMARY_RESCAN_SECONDS = 180
COMMENT_REPLY_RESCAN_SECONDS = 1800
COMMENT_MAX_RETRY_SECONDS = 43_200


@dataclass(frozen=True)
class CommentCaptureError:
    category: str
    code: str
    message: str


class ErrorClassifier(Protocol):
    def __call__(self, exc: Exception) -> CommentCaptureError: ...


class CommentRetryPolicy:
    def __init__(
        self,
        random_value: Callable[[], float],
        base_seconds: int = 60,
        cap_seconds: int = COMMENT_MAX_RETRY_SECONDS,
    ) -> None:
        self._random_value = random_value
        self._base_seconds = int(base_seconds)
        self._cap_seconds = int(cap_seconds)

    def delay_seconds(self, retry_count: int) -> int:
        raw = min(
            self._cap_seconds,
            self._base_seconds * (2 ** max(0, int(retry_count))),
        )
        jitter = 0.9 + (0.2 * float(self._random_value()))
        return min(
            self._cap_seconds,
            max(self._base_seconds, int(raw * jitter)),
        )


class CommentCaptureCoordinator:
    def __init__(
        self,
        gateway: BilibiliGateway,
        journal: CommentJournal,
        classify_error: ErrorClassifier,
        retry_policy: CommentRetryPolicy,
    ) -> None:
        self.gateway = gateway
        self.journal = journal
        self._classify_error = classify_error
        self._retry_policy = retry_policy

    async def run_scan_task(
        self,
        task: CommentScanTask,
        target_uids: Sequence[str],
        target_origins: Sequence[str],
        now: int,
    ) -> None:
        try:
            if task.kind == "primary":
                page = await self.gateway.get_root_comment_page(
                    task.resource, offset=task.cursor
                )
                self.journal.commit_scan_page(
                    task=task,
                    posts=page.posts,
                    target_uids=target_uids,
                    target_origins=target_origins,
                    now=now,
                    next_cursor=page.next_offset,
                    next_page_index=0,
                    next_sweep_at=(
                        now
                        if page.next_offset
                        else now + COMMENT_PRIMARY_RESCAN_SECONDS
                    ),
                )
                return

            page = await self.gateway.get_reply_comment_page(
                task.resource,
                root_id=task.root_rpid,
                page_index=task.page_index,
            )
            self.journal.commit_scan_page(
                task=task,
                posts=page.posts,
                target_uids=target_uids,
                target_origins=target_origins,
                now=now,
                next_cursor="",
                next_page_index=page.next_page_index,
                next_sweep_at=(
                    now
                    if page.next_page_index
                    else now + COMMENT_REPLY_RESCAN_SECONDS
                ),
            )
        except Exception as exc:
            error = self._classify_error(exc)
            delay = self._retry_policy.delay_seconds(task.retry_count)
            self.journal.mark_scan_failed(
                task.task_id,
                category=error.category,
                message=error.message,
                next_attempt_at=now + delay,
            )

    async def deliver_one(
        self,
        send: Callable[[str, BilibiliNotification], Awaitable[None]],
        now: int,
    ) -> bool:
        delivery = self.journal.next_due_delivery(now)
        if delivery is None:
            return False
        resource_text = (
            "动态" if delivery.resource.resource_kind == "dynamic" else "视频"
        )
        notification = BilibiliNotification(
            kind="comment",
            uid=delivery.post.author_uid,
            author_name=delivery.post.author_name,
            title="",
            url=delivery.resource.url,
            text=delivery.post.text,
            image_urls=list(delivery.post.image_urls),
            comment_created_at=delivery.post.created_at,
            comment_resource_owner_name=delivery.resource.owner_name,
            comment_resource_kind=resource_text,
            comment_resource_title=delivery.resource.title,
            comment_action_text=(
                "回复了评论" if delivery.post.is_reply else "发表了评论"
            ),
        )
        try:
            await send(delivery.unified_msg_origin, notification)
        except Exception as exc:
            error = self._classify_error(exc)
            delay = self._retry_policy.delay_seconds(delivery.attempt_count)
            self.journal.fail_delivery(
                delivery.delivery_id,
                error.category,
                error.message,
                now + delay,
            )
            return True
        self.journal.acknowledge_delivery(delivery.delivery_id, now)
        return True
