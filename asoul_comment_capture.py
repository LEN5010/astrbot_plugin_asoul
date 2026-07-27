from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, Sequence

from asoul_bilibili import BilibiliGateway, BilibiliNotification
from asoul_comment_journal import CommentJournal, CommentScanTask

COMMENT_PRIMARY_RESCAN_SECONDS = 180
COMMENT_RECONCILE_RESCAN_SECONDS = 6 * 60 * 60
COMMENT_MAX_RETRY_SECONDS = 43_200
COMMENT_PAGE_TIMEOUT_SECONDS = 20
COMMENT_SCAN_LANE_CYCLE = (
    "head",
    "reply",
    "head",
    "reconcile",
    "head",
    "reply",
    "head",
    "reconcile",
    "head",
    "reply",
)


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


class CommentWorkScheduler:
    def __init__(self) -> None:
        self._lane_index = 0
        self._owner_positions: dict[str, int] = {}

    def next_task(
        self,
        journal: CommentJournal,
        now: int,
        owner_uids: Sequence[str],
    ) -> CommentScanTask | None:
        preferred_lane = COMMENT_SCAN_LANE_CYCLE[
            self._lane_index % len(COMMENT_SCAN_LANE_CYCLE)
        ]
        self._lane_index += 1
        lane_order = [preferred_lane]
        lane_order.extend(
            lane
            for lane in ("head", "reply", "reconcile")
            if lane != preferred_lane
        )
        normalized_uids = tuple(
            dict.fromkeys(str(uid) for uid in owner_uids if str(uid))
        )
        for lane in lane_order:
            if not normalized_uids:
                task = journal.next_due_scan_task(now, lane=lane)
                if task is not None:
                    return task
                continue
            start = self._owner_positions.get(lane, 0) % len(normalized_uids)
            for offset in range(len(normalized_uids)):
                position = (start + offset) % len(normalized_uids)
                task = journal.next_due_scan_task(
                    now,
                    lane=lane,
                    owner_uid=normalized_uids[position],
                )
                if task is None:
                    continue
                self._owner_positions[lane] = position + 1
                return task
        return None


class CommentCaptureCoordinator:
    def __init__(
        self,
        gateway: BilibiliGateway,
        journal: CommentJournal,
        classify_error: ErrorClassifier,
        retry_policy: CommentRetryPolicy,
        request_timeout_seconds: float = COMMENT_PAGE_TIMEOUT_SECONDS,
    ) -> None:
        self.gateway = gateway
        self.journal = journal
        self._classify_error = classify_error
        self._retry_policy = retry_policy
        self._request_timeout_seconds = max(0.01, float(request_timeout_seconds))

    async def run_scan_task(
        self,
        task: CommentScanTask,
        target_uids: Sequence[str],
        target_origins: Sequence[str],
        now: int,
    ) -> None:
        try:
            if task.kind == "primary":
                page = await asyncio.wait_for(
                    self.gateway.get_root_comment_page(
                        task.resource, offset=task.cursor
                    ),
                    timeout=self._request_timeout_seconds,
                )
                root_ids = [state.root_rpid for state in page.root_states]
                page_reaches_known_root = self.journal.has_observed_root(
                    task.lifecycle_id, root_ids
                )
                if task.scan_lane == "head":
                    should_continue = (
                        task.lifecycle_state != "bootstrapping"
                        and bool(page.next_offset)
                        and not page_reaches_known_root
                    )
                    next_cursor = page.next_offset if should_continue else ""
                    next_sweep_at = (
                        now if should_continue else now + COMMENT_PRIMARY_RESCAN_SECONDS
                    )
                else:
                    next_cursor = page.next_offset
                    next_sweep_at = (
                        now
                        if page.next_offset
                        else now + COMMENT_RECONCILE_RESCAN_SECONDS
                    )
                self.journal.commit_scan_page(
                    task=task,
                    posts=page.posts,
                    root_states=page.root_states,
                    target_uids=target_uids,
                    target_origins=target_origins,
                    now=now,
                    next_cursor=next_cursor,
                    next_page_index=0,
                    next_sweep_at=next_sweep_at,
                )
                return

            page = await asyncio.wait_for(
                self.gateway.get_reply_comment_page(
                    task.resource,
                    root_id=task.root_rpid,
                    page_index=task.page_index,
                ),
                timeout=self._request_timeout_seconds,
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
                    else 0
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
                attempted_at=now,
                code=getattr(error, "code", "") or "",
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
            content_id=delivery.post.id,
            published_at=delivery.post.created_at,
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
