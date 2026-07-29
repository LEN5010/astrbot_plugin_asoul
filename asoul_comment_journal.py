from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from asoul_bilibili import (
    BilibiliCommentPost,
    BilibiliCommentResource,
    BilibiliRichTextNode,
    BilibiliRootReplyState,
)


COMMENT_HEAD_ROOT_SENTINEL = "__head__"
# Fallback only when resource_published_at is missing on legacy rows.
COMMENT_BASELINE_GRACE_SECONDS = 15 * 60
COMMENT_NOTIFICATION_MAX_AGE_SECONDS = 24 * 60 * 60
# Kept for status/compat imports; periodic safety activation is disabled.
COMMENT_REPLY_SAFETY_MIN_SECONDS = 24 * 60 * 60
COMMENT_REPLY_SAFETY_DAILY_BUDGET = 10_000

DELETED_COMMENT_ERROR_MARKERS = (
    "已经被删除",
    "已删除",
    "没有该评论",
    "评论不存在",
    "评论区已关闭",
)
DELETED_COMMENT_ERROR_CODES = frozenset({"12002", "12006", "-404", "404"})


@dataclass(frozen=True)
class CommentResourceLifecycle:
    lifecycle_id: str
    resource: BilibiliCommentResource
    entered_at: int
    retired_at: int
    state: str
    incomplete_reason: str
    head_ready_at: int = 0
    baseline_completed_at: int = 0
    resource_published_at: int = 0


@dataclass(frozen=True)
class CommentScanTask:
    task_id: int
    lifecycle_id: str
    owner_uid: str
    entered_at: int
    lifecycle_state: str
    resource: BilibiliCommentResource
    kind: str
    root_rpid: str
    cursor: str
    page_index: int
    retry_count: int
    scan_lane: str = "reconcile"
    task_state: str = "scheduled"
    last_attempt_at: int = 0
    reply_change_pending: bool = False


@dataclass(frozen=True)
class CatalogSyncResult:
    activated: tuple[CommentResourceLifecycle, ...]
    retired: tuple[CommentResourceLifecycle, ...]


@dataclass(frozen=True)
class PageCommitResult:
    events_created: int
    deliveries_created: int
    roots_enqueued: int
    lifecycle_activated: bool


@dataclass(frozen=True)
class PendingCommentDelivery:
    delivery_id: int
    event_id: int
    unified_msg_origin: str
    resource: BilibiliCommentResource
    post: BilibiliCommentPost
    attempt_count: int


@dataclass(frozen=True)
class CommentJournalStatus:
    lifecycle_counts: dict[str, int]
    incomplete_count: int
    pending_scan_count: int
    overdue_scan_count: int
    retrying_scan_count: int
    oldest_scan_due_at: int
    pending_delivery_count: int
    oldest_delivery_due_at: int
    last_reconciliation_at: int
    lane_due_counts: dict[str, int] | None = None
    dormant_reply_count: int = 0
    reply_change_pending_count: int = 0
    reply_continuation_count: int = 0
    reply_retrying_count: int = 0
    baseline_pending_count: int = 0
    oldest_head_due_at: int = 0
    last_root_reconciliation_at: int = 0
    last_reply_reconciliation_at: int = 0
    request_count_15m: int = 0
    request_count_60m: int = 0
    reply_safety_interval_seconds: int = 0
    owner_last_attempt_at: dict[str, int] | None = None
    reply_gap_count: int = 0
    terminal_reply_count: int = 0


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS owner_catalog (
    owner_uid TEXT PRIMARY KEY,
    author_name TEXT NOT NULL DEFAULT '',
    last_attempt_at INTEGER NOT NULL DEFAULT 0,
    last_success_at INTEGER NOT NULL DEFAULT 0,
    last_error_category TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS resource_lifecycle (
    lifecycle_id TEXT PRIMARY KEY,
    owner_uid TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    resource_kind TEXT NOT NULL,
    oid INTEGER NOT NULL,
    type_value INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    entered_at INTEGER NOT NULL,
    retired_at INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL CHECK(state IN ('bootstrapping', 'active', 'retired')),
    incomplete_reason TEXT NOT NULL DEFAULT '',
    head_ready_at INTEGER NOT NULL DEFAULT 0,
    baseline_completed_at INTEGER NOT NULL DEFAULT 0,
    resource_published_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_resource
ON resource_lifecycle(owner_uid, resource_key)
WHERE retired_at = 0;
CREATE TABLE IF NOT EXISTS scan_task (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lifecycle_id TEXT NOT NULL REFERENCES resource_lifecycle(lifecycle_id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('primary', 'reply')),
    root_rpid TEXT NOT NULL DEFAULT '',
    cursor TEXT NOT NULL DEFAULT '',
    page_index INTEGER NOT NULL DEFAULT 1,
    bootstrap_pending INTEGER NOT NULL DEFAULT 1,
    next_attempt_at INTEGER NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_success_at INTEGER NOT NULL DEFAULT 0,
    last_error_category TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    scan_lane TEXT NOT NULL DEFAULT 'reconcile',
    task_state TEXT NOT NULL DEFAULT 'scheduled',
    last_attempt_at INTEGER NOT NULL DEFAULT 0,
    reply_change_pending INTEGER NOT NULL DEFAULT 0,
    UNIQUE(lifecycle_id, kind, root_rpid)
);
CREATE INDEX IF NOT EXISTS ix_scan_due ON scan_task(next_attempt_at, task_id);
CREATE TABLE IF NOT EXISTS observed_comment (
    lifecycle_id TEXT NOT NULL REFERENCES resource_lifecycle(lifecycle_id) ON DELETE CASCADE,
    rpid TEXT NOT NULL,
    author_uid TEXT NOT NULL,
    author_name TEXT NOT NULL,
    text TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    is_reply INTEGER NOT NULL,
    root_rpid TEXT NOT NULL,
    parent_rpid TEXT NOT NULL,
    image_urls_json TEXT NOT NULL,
    rich_nodes_json TEXT NOT NULL DEFAULT '[]',
    baseline INTEGER NOT NULL,
    observed_at INTEGER NOT NULL,
    PRIMARY KEY(lifecycle_id, rpid)
);
CREATE INDEX IF NOT EXISTS ix_observed_root_reply
ON observed_comment(lifecycle_id, root_rpid, is_reply);
CREATE TABLE IF NOT EXISTS comment_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lifecycle_id TEXT NOT NULL,
    rpid TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    UNIQUE(lifecycle_id, rpid),
    FOREIGN KEY(lifecycle_id, rpid)
        REFERENCES observed_comment(lifecycle_id, rpid) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS event_delivery (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES comment_event(event_id) ON DELETE CASCADE,
    unified_msg_origin TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending', 'acknowledged', 'cancelled')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_error_category TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    acknowledged_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE(event_id, unified_msg_origin)
);
CREATE INDEX IF NOT EXISTS ix_delivery_due
ON event_delivery(state, next_attempt_at, delivery_id);
CREATE TABLE IF NOT EXISTS comment_root_state (
    lifecycle_id TEXT NOT NULL REFERENCES resource_lifecycle(lifecycle_id) ON DELETE CASCADE,
    root_rpid TEXT NOT NULL,
    root_created_at INTEGER NOT NULL DEFAULT 0,
    known_reply_count INTEGER NOT NULL DEFAULT -1,
    reconciled_reply_count INTEGER NOT NULL DEFAULT -1,
    embedded_reply_ids_json TEXT NOT NULL DEFAULT '[]',
    last_seen_at INTEGER NOT NULL DEFAULT 0,
    last_reply_scan_at INTEGER NOT NULL DEFAULT 0,
    next_safety_scan_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(lifecycle_id, root_rpid)
);
CREATE INDEX IF NOT EXISTS ix_root_safety_due
ON comment_root_state(next_safety_scan_at, lifecycle_id, root_rpid);
CREATE TABLE IF NOT EXISTS comment_scan_attempt (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    lifecycle_id TEXT NOT NULL,
    owner_uid TEXT NOT NULL,
    scan_lane TEXT NOT NULL,
    attempted_at INTEGER NOT NULL,
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_comment_attempt_time
ON comment_scan_attempt(attempted_at, scan_lane);
CREATE TABLE IF NOT EXISTS comment_scan_minute (
    minute_started_at INTEGER PRIMARY KEY,
    request_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS comment_schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class CommentJournal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA_SQL)
        self._migrate_schema()
        self._connection.commit()

    def _migrate_schema(self) -> None:
        def column_names(table: str) -> set[str]:
            return {
                str(row["name"])
                for row in self._connection.execute(
                    f"PRAGMA table_info({table})"
                ).fetchall()
            }

        with self._connection:
            catalog_columns = column_names("owner_catalog")
            if "retry_count" not in catalog_columns:
                self._connection.execute(
                    "ALTER TABLE owner_catalog ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
                )
            if "next_attempt_at" not in catalog_columns:
                self._connection.execute(
                    "ALTER TABLE owner_catalog ADD COLUMN next_attempt_at INTEGER NOT NULL DEFAULT 0"
                )

            lifecycle_columns = column_names("resource_lifecycle")
            if "head_ready_at" not in lifecycle_columns:
                self._connection.execute(
                    "ALTER TABLE resource_lifecycle ADD COLUMN head_ready_at INTEGER NOT NULL DEFAULT 0"
                )
            if "baseline_completed_at" not in lifecycle_columns:
                self._connection.execute(
                    "ALTER TABLE resource_lifecycle ADD COLUMN baseline_completed_at INTEGER NOT NULL DEFAULT 0"
                )
            if "resource_published_at" not in lifecycle_columns:
                self._connection.execute(
                    "ALTER TABLE resource_lifecycle ADD COLUMN resource_published_at INTEGER NOT NULL DEFAULT 0"
                )

            scan_columns = column_names("scan_task")
            if "scan_lane" not in scan_columns:
                self._connection.execute(
                    "ALTER TABLE scan_task ADD COLUMN scan_lane TEXT NOT NULL DEFAULT 'reconcile'"
                )
            if "task_state" not in scan_columns:
                self._connection.execute(
                    "ALTER TABLE scan_task ADD COLUMN task_state TEXT NOT NULL DEFAULT 'scheduled'"
                )
            if "last_attempt_at" not in scan_columns:
                self._connection.execute(
                    "ALTER TABLE scan_task ADD COLUMN last_attempt_at INTEGER NOT NULL DEFAULT 0"
                )
            if "reply_change_pending" not in scan_columns:
                self._connection.execute(
                    "ALTER TABLE scan_task ADD COLUMN reply_change_pending INTEGER NOT NULL DEFAULT 0"
                )
            observed_columns = column_names("observed_comment")
            if "rich_nodes_json" not in observed_columns:
                self._connection.execute(
                    "ALTER TABLE observed_comment ADD COLUMN rich_nodes_json TEXT NOT NULL DEFAULT '[]'"
                )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_scan_lane_due
                ON scan_task(task_state, scan_lane, next_attempt_at, task_id)
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_scan_reply_gap
                ON scan_task(task_state, kind, lifecycle_id, root_rpid)
                """
            )

            version_row = self._connection.execute(
                "SELECT value FROM comment_schema_meta WHERE key = 'capacity_scheduler_version'"
            ).fetchone()
            version = int(version_row["value"]) if version_row is not None else 0
            if version < 1:
                self._connection.execute(
                    "UPDATE scan_task SET scan_lane = CASE WHEN kind = 'reply' THEN 'reply' ELSE 'reconcile' END"
                )
                self._connection.execute(
                    """
                    UPDATE scan_task
                    SET task_state = CASE
                        WHEN kind = 'reply' AND page_index = 1 AND retry_count = 0
                            THEN 'dormant'
                        ELSE 'scheduled'
                    END
                    """
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO comment_root_state(
                        lifecycle_id, root_rpid, known_reply_count,
                        reconciled_reply_count
                    )
                    SELECT lifecycle_id, root_rpid, -1, -1
                    FROM scan_task WHERE kind = 'reply'
                    """
                )
                self._connection.execute(
                    """
                    INSERT OR IGNORE INTO scan_task(
                        lifecycle_id, kind, root_rpid, cursor, page_index,
                        bootstrap_pending, next_attempt_at, scan_lane,
                        task_state
                    )
                    SELECT lifecycle_id, 'primary', ?, '', 1, 0,
                           CASE WHEN head_ready_at > 0 THEN head_ready_at ELSE entered_at END,
                           'head', 'scheduled'
                    FROM resource_lifecycle WHERE retired_at = 0
                    """,
                    (COMMENT_HEAD_ROOT_SENTINEL,),
                )
                self._connection.execute(
                    """
                    INSERT INTO comment_schema_meta(key, value) VALUES(
                        'capacity_scheduler_version', '1'
                    )
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """
                )

    def close(self) -> None:
        self._connection.close()

    def catalog_refresh_due(
        self, owner_uid: str, now: int, interval_seconds: int
    ) -> bool:
        row = self._connection.execute(
            """
            SELECT last_attempt_at, next_attempt_at
            FROM owner_catalog WHERE owner_uid = ?
            """,
            (str(owner_uid),),
        ).fetchone()
        if row is None:
            return True
        next_attempt_at = int(row["next_attempt_at"])
        if next_attempt_at > 0:
            return int(now) >= next_attempt_at
        return int(now) - int(row["last_attempt_at"]) >= int(interval_seconds)

    def catalog_retry_count(self, owner_uid: str) -> int:
        row = self._connection.execute(
            "SELECT retry_count FROM owner_catalog WHERE owner_uid = ?",
            (str(owner_uid),),
        ).fetchone()
        return int(row["retry_count"]) if row is not None else 0

    def begin_catalog_refresh(self, owner_uid: str, now: int) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO owner_catalog(owner_uid, last_attempt_at)
                VALUES(?, ?)
                ON CONFLICT(owner_uid) DO UPDATE
                SET last_attempt_at = excluded.last_attempt_at
                """,
                (str(owner_uid), int(now)),
            )

    def fail_catalog_refresh(
        self,
        owner_uid: str,
        category: str,
        message: str,
        next_attempt_at: int,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE owner_catalog
                SET last_error_category = ?, last_error_message = ?,
                    retry_count = retry_count + 1,
                    next_attempt_at = ?
                WHERE owner_uid = ?
                """,
                (
                    str(category),
                    str(message),
                    int(next_attempt_at),
                    str(owner_uid),
                ),
            )

    def sync_resource_catalog(
        self,
        owner_uid: str,
        author_name: str,
        resources: Sequence[BilibiliCommentResource],
        now: int,
    ) -> CatalogSyncResult:
        normalized_uid = str(owner_uid)
        normalized_name = str(author_name or normalized_uid)
        current_resources = {
            resource.key: resource
            for resource in resources
            if str(resource.key or "").strip()
        }
        activated: list[CommentResourceLifecycle] = []
        retired: list[CommentResourceLifecycle] = []

        with self._connection:
            self._connection.execute(
                """
                INSERT INTO owner_catalog(
                    owner_uid, author_name, last_attempt_at, last_success_at,
                    last_error_category, last_error_message
                ) VALUES(?, ?, ?, ?, '', '')
                ON CONFLICT(owner_uid) DO UPDATE SET
                    author_name = excluded.author_name,
                    last_success_at = excluded.last_success_at,
                    last_error_category = '',
                    last_error_message = '', retry_count = 0,
                    next_attempt_at = 0
                """,
                (normalized_uid, normalized_name, int(now), int(now)),
            )
            active_rows = self._connection.execute(
                """
                SELECT * FROM resource_lifecycle
                WHERE owner_uid = ? AND retired_at = 0
                """,
                (normalized_uid,),
            ).fetchall()
            active_by_key = {str(row["resource_key"]): row for row in active_rows}

            for resource_key, row in active_by_key.items():
                if resource_key in current_resources:
                    resource = current_resources[resource_key]
                    published_at = max(0, int(resource.published_at or 0))
                    self._connection.execute(
                        """
                        UPDATE resource_lifecycle SET
                            owner_name = ?, resource_kind = ?, oid = ?,
                            type_value = ?, title = ?, url = ?,
                            resource_published_at = CASE
                                WHEN ? > 0 THEN ?
                                ELSE resource_published_at
                            END
                        WHERE lifecycle_id = ?
                        """,
                        (
                            normalized_name,
                            resource.resource_kind,
                            int(resource.oid),
                            int(resource.type_value),
                            resource.title,
                            resource.url,
                            published_at,
                            published_at,
                            str(row["lifecycle_id"]),
                        ),
                    )
                    continue
                retired.append(self._retire_lifecycle(row, int(now)))

            for resource_key, resource in current_resources.items():
                if resource_key in active_by_key:
                    continue
                lifecycle_id = uuid.uuid4().hex
                published_at = max(0, int(resource.published_at or 0))
                self._connection.execute(
                    """
                    INSERT INTO resource_lifecycle(
                        lifecycle_id, owner_uid, owner_name, resource_key,
                        resource_kind, oid, type_value, title, url, entered_at,
                        retired_at, state, incomplete_reason,
                        resource_published_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'bootstrapping', '', ?)
                    """,
                    (
                        lifecycle_id,
                        normalized_uid,
                        normalized_name,
                        resource.key,
                        resource.resource_kind,
                        int(resource.oid),
                        int(resource.type_value),
                        resource.title,
                        resource.url,
                        int(now),
                        published_at,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO scan_task(
                        lifecycle_id, kind, root_rpid, cursor, page_index,
                        bootstrap_pending, next_attempt_at, scan_lane,
                        task_state
                    ) VALUES(?, 'primary', '', '', 1, 1, ?,
                             'reconcile', 'scheduled')
                    """,
                    (lifecycle_id, int(now)),
                )
                self._connection.execute(
                    """
                    INSERT INTO scan_task(
                        lifecycle_id, kind, root_rpid, cursor, page_index,
                        bootstrap_pending, next_attempt_at, scan_lane,
                        task_state
                    ) VALUES(?, 'primary', ?, '', 1, 0, ?,
                             'head', 'scheduled')
                    """,
                    (lifecycle_id, COMMENT_HEAD_ROOT_SENTINEL, int(now)),
                )
                lifecycle_row = self._connection.execute(
                    "SELECT * FROM resource_lifecycle WHERE lifecycle_id = ?",
                    (lifecycle_id,),
                ).fetchone()
                activated.append(self._row_to_lifecycle(lifecycle_row))

        return CatalogSyncResult(tuple(activated), tuple(retired))

    def retire_unconfigured_owners(
        self, active_owner_uids: Sequence[str], now: int
    ) -> tuple[CommentResourceLifecycle, ...]:
        allowed = {str(uid) for uid in active_owner_uids}
        retired: list[CommentResourceLifecycle] = []
        with self._connection:
            rows = self._connection.execute(
                "SELECT * FROM resource_lifecycle WHERE retired_at = 0"
            ).fetchall()
            for row in rows:
                if str(row["owner_uid"]) in allowed:
                    continue
                retired.append(self._retire_lifecycle(row, int(now)))
        return tuple(retired)

    def _retire_lifecycle(
        self, row: sqlite3.Row, now: int
    ) -> CommentResourceLifecycle:
        lifecycle_id = str(row["lifecycle_id"])
        unfinished = self._connection.execute(
            """
            SELECT 1 FROM scan_task
            WHERE lifecycle_id = ? AND (
                retry_count > 0
                OR (kind = 'primary' AND cursor != '')
                OR (kind = 'reply' AND page_index > 1)
            )
            LIMIT 1
            """,
            (lifecycle_id,),
        ).fetchone()
        incomplete_reason = "retired with unfinished scan" if unfinished else ""
        self._connection.execute(
            """
            UPDATE resource_lifecycle
            SET retired_at = ?, state = 'retired', incomplete_reason = ?
            WHERE lifecycle_id = ?
            """,
            (int(now), incomplete_reason, lifecycle_id),
        )
        self._connection.execute(
            """
            UPDATE scan_task
            SET task_state = 'retired'
            WHERE lifecycle_id = ?
            """,
            (lifecycle_id,),
        )
        retired_row = self._connection.execute(
            "SELECT * FROM resource_lifecycle WHERE lifecycle_id = ?",
            (lifecycle_id,),
        ).fetchone()
        return self._row_to_lifecycle(retired_row)

    def next_due_scan_task(
        self,
        now: int,
        *,
        lane: str | None = None,
        owner_uid: str | None = None,
    ) -> CommentScanTask | None:
        filters = [
            "resource_lifecycle.retired_at = 0",
            "scan_task.task_state = 'scheduled'",
            "scan_task.next_attempt_at <= ?",
        ]
        parameters: list[object] = [int(now)]
        if lane:
            filters.append("scan_task.scan_lane = ?")
            parameters.append(str(lane))
        if owner_uid:
            filters.append("resource_lifecycle.owner_uid = ?")
            parameters.append(str(owner_uid))
        where_sql = " AND ".join(filters)
        row = self._connection.execute(
            f"""
            SELECT
                scan_task.task_id,
                scan_task.lifecycle_id,
                scan_task.kind,
                scan_task.root_rpid,
                scan_task.cursor,
                scan_task.page_index,
                scan_task.retry_count,
                scan_task.scan_lane,
                scan_task.task_state,
                scan_task.last_attempt_at,
                scan_task.reply_change_pending,
                resource_lifecycle.owner_uid,
                resource_lifecycle.owner_name,
                resource_lifecycle.resource_key,
                resource_lifecycle.resource_kind,
                resource_lifecycle.oid,
                resource_lifecycle.type_value,
                resource_lifecycle.title,
                resource_lifecycle.url,
                resource_lifecycle.entered_at,
                resource_lifecycle.state AS lifecycle_state
            FROM scan_task
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE {where_sql}
            ORDER BY
              CASE
                WHEN scan_task.retry_count > 0 THEN 0
                WHEN scan_task.kind = 'reply' AND scan_task.page_index > 1 THEN 1
                WHEN scan_task.kind = 'primary' AND scan_task.cursor != '' THEN 1
                WHEN scan_task.kind = 'reply'
                     AND scan_task.reply_change_pending = 1 THEN 2
                ELSE 3
              END,
              scan_task.next_attempt_at,
              scan_task.task_id
            LIMIT 1
            """,
            tuple(parameters),
        ).fetchone()
        if row is None:
            return None
        return CommentScanTask(
            task_id=int(row["task_id"]),
            lifecycle_id=str(row["lifecycle_id"]),
            owner_uid=str(row["owner_uid"]),
            entered_at=int(row["entered_at"]),
            lifecycle_state=str(row["lifecycle_state"]),
            resource=self._resource_from_row(row),
            kind=str(row["kind"]),
            root_rpid=str(row["root_rpid"]),
            cursor=str(row["cursor"]),
            page_index=int(row["page_index"]),
            retry_count=int(row["retry_count"]),
            scan_lane=str(row["scan_lane"]),
            task_state=str(row["task_state"]),
            last_attempt_at=int(row["last_attempt_at"]),
            reply_change_pending=bool(row["reply_change_pending"]),
        )

    def commit_scan_page(
        self,
        task: CommentScanTask,
        posts: Sequence[BilibiliCommentPost],
        target_uids: Sequence[str],
        target_origins: Sequence[str],
        now: int,
        *,
        root_states: Sequence[BilibiliRootReplyState] = (),
        next_cursor: str,
        next_page_index: int,
        next_sweep_at: int,
    ) -> PageCommitResult:
        for post in posts:
            if not str(post.id or "").strip():
                raise ValueError("comment must have a non-empty rpid")
            if int(post.created_at or 0) <= 0:
                raise ValueError("comment must have a valid ctime")

        normalized_target_uids = {str(uid) for uid in target_uids}
        normalized_origins = tuple(
            dict.fromkeys(str(origin) for origin in target_origins if str(origin))
        )
        events_created = 0
        deliveries_created = 0
        roots_enqueued = 0
        lifecycle_activated = False

        with self._connection:
            lifecycle = self._connection.execute(
                "SELECT * FROM resource_lifecycle WHERE lifecycle_id = ?",
                (str(task.lifecycle_id),),
            ).fetchone()
            if lifecycle is None:
                raise ValueError("comment resource lifecycle does not exist")
            if int(lifecycle["retired_at"]) or str(lifecycle["state"]) == "retired":
                raise ValueError("comment resource lifecycle is retired")

            lifecycle_state = str(lifecycle["state"])
            entered_at = int(lifecycle["entered_at"])
            try:
                resource_published_at = int(lifecycle["resource_published_at"] or 0)
            except (KeyError, IndexError, TypeError, ValueError):
                resource_published_at = 0
            baseline_cutoff = self._baseline_cutoff(
                resource_published_at=resource_published_at,
                entered_at=entered_at,
            )
            for post in posts:
                baseline = int(int(post.created_at) < baseline_cutoff)
                inserted = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO observed_comment(
                        lifecycle_id, rpid, author_uid, author_name, text,
                        created_at, is_reply, root_rpid, parent_rpid,
                        image_urls_json, rich_nodes_json, baseline, observed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(task.lifecycle_id),
                        str(post.id),
                        str(post.author_uid),
                        str(post.author_name),
                        str(post.text),
                        int(post.created_at),
                        int(bool(post.is_reply)),
                        str(post.root_id or post.id),
                        str(post.parent_id or ""),
                        json.dumps(list(post.image_urls), ensure_ascii=False),
                        json.dumps(
                            [
                                {
                                    "kind": node.kind,
                                    "text": node.text,
                                    "image_url": node.image_url,
                                    "url": node.url,
                                }
                                for node in post.rich_nodes
                            ],
                            ensure_ascii=False,
                        ),
                        baseline,
                        int(now),
                    ),
                )
                if (
                    inserted.rowcount == 1
                    and not baseline
                    and int(post.created_at)
                    >= int(now) - COMMENT_NOTIFICATION_MAX_AGE_SECONDS
                    and str(post.author_uid) in normalized_target_uids
                ):
                    event_cursor = self._connection.execute(
                        """
                        INSERT OR IGNORE INTO comment_event(
                            lifecycle_id, rpid, captured_at
                        ) VALUES(?, ?, ?)
                        """,
                        (str(task.lifecycle_id), str(post.id), int(now)),
                    )
                    if event_cursor.rowcount == 1:
                        events_created += 1
                        event_id = int(event_cursor.lastrowid)
                        for origin in normalized_origins:
                            delivery_cursor = self._connection.execute(
                                """
                                INSERT OR IGNORE INTO event_delivery(
                                    event_id, unified_msg_origin, state,
                                    next_attempt_at
                                ) VALUES(?, ?, 'pending', ?)
                                """,
                                (event_id, origin, int(now)),
                            )
                            deliveries_created += max(0, delivery_cursor.rowcount)

            if task.kind == "primary":
                normalized_root_states = self._normalize_root_states(
                    posts, root_states
                )
                root_created_at = {
                    str(post.id): int(post.created_at)
                    for post in posts
                    if not post.is_reply
                }
                for root_state in normalized_root_states:
                    roots_enqueued += self._upsert_root_state(
                        lifecycle_id=str(task.lifecycle_id),
                        root_state=root_state,
                        root_created_at=root_created_at.get(
                            str(root_state.root_rpid), 0
                        ),
                        now=int(now),
                    )

            stream_exhausted = (
                not str(next_cursor or "")
                if task.kind == "primary"
                else int(next_page_index or 0) <= 0
            )
            if task.kind == "primary":
                self._connection.execute(
                    """
                    UPDATE scan_task SET
                        cursor = ?, page_index = 1,
                        bootstrap_pending = CASE WHEN ? THEN 0 ELSE bootstrap_pending END,
                        next_attempt_at = ?, retry_count = 0,
                        last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                        last_error_category = '', last_error_message = '',
                        task_state = 'scheduled', last_attempt_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        "" if stream_exhausted else str(next_cursor),
                        int(stream_exhausted),
                        int(next_sweep_at),
                        int(stream_exhausted),
                        int(now),
                        int(now),
                        int(task.task_id),
                    ),
                )
                if task.scan_lane == "head":
                    self._connection.execute(
                        """
                        UPDATE resource_lifecycle
                        SET state = 'active',
                            head_ready_at = CASE
                                WHEN head_ready_at > 0 THEN head_ready_at ELSE ? END
                        WHERE lifecycle_id = ? AND retired_at = 0
                        """,
                        (int(now), str(task.lifecycle_id)),
                    )
                    lifecycle_activated = lifecycle_state == "bootstrapping"
                elif task.scan_lane == "reconcile" and stream_exhausted:
                    self._connection.execute(
                        """
                        UPDATE resource_lifecycle
                        SET baseline_completed_at = CASE
                            WHEN baseline_completed_at > 0
                                THEN baseline_completed_at ELSE ? END
                        WHERE lifecycle_id = ?
                        """,
                        (int(now), str(task.lifecycle_id)),
                    )
            else:
                self._connection.execute(
                    """
                    UPDATE scan_task SET
                        cursor = '', page_index = ?,
                        bootstrap_pending = CASE WHEN ? THEN 0 ELSE bootstrap_pending END,
                        next_attempt_at = ?, retry_count = 0,
                        last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                        last_error_category = '', last_error_message = '',
                        task_state = ?, last_attempt_at = ?,
                        reply_change_pending = 0
                    WHERE task_id = ?
                    """,
                    (
                        1 if stream_exhausted else int(next_page_index),
                        int(stream_exhausted),
                        0 if stream_exhausted else int(next_sweep_at),
                        int(stream_exhausted),
                        int(now),
                        "dormant" if stream_exhausted else "scheduled",
                        int(now),
                        int(task.task_id),
                    ),
                )
                if stream_exhausted:
                    # Full reply walk finished: archive is source of truth until
                    # a later primary page reports a higher rcount/fingerprint.
                    observed_replies = self._observed_reply_count(
                        str(task.lifecycle_id), str(task.root_rpid)
                    )
                    self._connection.execute(
                        """
                        UPDATE comment_root_state
                        SET known_reply_count = ?,
                            reconciled_reply_count = ?,
                            last_reply_scan_at = ?, next_safety_scan_at = 0
                        WHERE lifecycle_id = ? AND root_rpid = ?
                        """,
                        (
                            observed_replies,
                            observed_replies,
                            int(now),
                            str(task.lifecycle_id),
                            str(task.root_rpid),
                        ),
                    )

            self._record_scan_attempt(int(now))

        return PageCommitResult(
            events_created=events_created,
            deliveries_created=deliveries_created,
            roots_enqueued=roots_enqueued,
            lifecycle_activated=lifecycle_activated,
        )

    @staticmethod
    def _normalize_root_states(
        posts: Sequence[BilibiliCommentPost],
        root_states: Sequence[BilibiliRootReplyState],
    ) -> tuple[BilibiliRootReplyState, ...]:
        supplied = {
            str(state.root_rpid): state
            for state in root_states
            if str(state.root_rpid or "").strip()
        }
        embedded_by_root: dict[str, list[str]] = {}
        for post in posts:
            if not post.is_reply:
                continue
            embedded_by_root.setdefault(str(post.root_id), []).append(str(post.id))
        for post in posts:
            if post.is_reply or str(post.id) in supplied:
                continue
            supplied[str(post.id)] = BilibiliRootReplyState(
                root_rpid=str(post.id),
                reply_count=max(int(post.reply_count or 0), len(embedded_by_root.get(str(post.id), []))),
                embedded_reply_ids=tuple(embedded_by_root.get(str(post.id), [])),
            )
        return tuple(supplied.values())

    def _upsert_root_state(
        self,
        *,
        lifecycle_id: str,
        root_state: BilibiliRootReplyState,
        root_created_at: int,
        now: int,
    ) -> int:
        root_rpid = str(root_state.root_rpid)
        reply_count = max(0, int(root_state.reply_count or 0))
        normalized_embedded_ids = sorted(
            {str(value) for value in root_state.embedded_reply_ids if str(value)},
            key=lambda value: (int(value) if value.isdigit() else 0, value),
        )
        fingerprint = json.dumps(
            normalized_embedded_ids,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        previous = self._connection.execute(
            """
            SELECT known_reply_count, reconciled_reply_count, embedded_reply_ids_json
            FROM comment_root_state
            WHERE lifecycle_id = ? AND root_rpid = ?
            """,
            (lifecycle_id, root_rpid),
        ).fetchone()
        observed_replies = self._observed_reply_count(lifecycle_id, root_rpid)
        previous_known = (
            int(previous["known_reply_count"]) if previous is not None else -1
        )
        previous_reconciled = (
            int(previous["reconciled_reply_count"]) if previous is not None else -1
        )
        previous_fingerprint = (
            str(previous["embedded_reply_ids_json"]) if previous is not None else ""
        )
        count_changed = previous is None or previous_known != reply_count
        fingerprint_changed = previous_fingerprint != fingerprint
        gap_vs_observed = reply_count > observed_replies
        gap_vs_reconciled = (
            reply_count > 0
            and previous_reconciled >= 0
            and reply_count > previous_reconciled
        )
        needs_scan = reply_count > 0 and (
            previous is None
            or count_changed
            or fingerprint_changed
            or gap_vs_observed
            or gap_vs_reconciled
        )
        reconciled_count = 0 if reply_count == 0 else -1
        self._connection.execute(
            """
            INSERT INTO comment_root_state(
                lifecycle_id, root_rpid, root_created_at,
                known_reply_count, reconciled_reply_count,
                embedded_reply_ids_json, last_seen_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lifecycle_id, root_rpid) DO UPDATE SET
                root_created_at = CASE
                    WHEN comment_root_state.root_created_at > 0
                        THEN comment_root_state.root_created_at
                    ELSE excluded.root_created_at END,
                known_reply_count = excluded.known_reply_count,
                reconciled_reply_count = CASE
                    WHEN excluded.known_reply_count = 0 THEN 0
                    ELSE comment_root_state.reconciled_reply_count END,
                embedded_reply_ids_json = excluded.embedded_reply_ids_json,
                last_seen_at = excluded.last_seen_at
            """,
            (
                lifecycle_id,
                root_rpid,
                max(0, int(root_created_at)),
                reply_count,
                reconciled_count,
                fingerprint,
                int(now),
            ),
        )
        existing_task = self._connection.execute(
            """
            SELECT task_id, task_state, page_index, retry_count
            FROM scan_task
            WHERE lifecycle_id = ? AND kind = 'reply' AND root_rpid = ?
            """,
            (lifecycle_id, root_rpid),
        ).fetchone()
        desired_state = "scheduled" if needs_scan else "dormant"
        if existing_task is None:
            cursor = self._connection.execute(
                """
                INSERT INTO scan_task(
                    lifecycle_id, kind, root_rpid, cursor, page_index,
                    bootstrap_pending, next_attempt_at, scan_lane,
                    task_state, reply_change_pending
                ) VALUES(?, 'reply', ?, '', 1, 0, ?, 'reply', ?, ?)
                """,
                (
                    lifecycle_id,
                    root_rpid,
                    int(now) if desired_state == "scheduled" else 0,
                    desired_state,
                    int(desired_state == "scheduled"),
                ),
            )
            return max(0, cursor.rowcount)

        if str(existing_task["task_state"]) == "retired":
            return 0

        in_progress = (
            int(existing_task["page_index"]) > 1
            or int(existing_task["retry_count"]) > 0
        )
        if reply_count <= 0 and not in_progress and not needs_scan:
            self._connection.execute(
                """
                UPDATE scan_task
                SET task_state = 'dormant', page_index = 1,
                    next_attempt_at = 0, reply_change_pending = 0
                WHERE task_id = ?
                """,
                (int(existing_task["task_id"]),),
            )
        elif needs_scan and not in_progress:
            self._connection.execute(
                """
                UPDATE scan_task
                SET task_state = 'scheduled', page_index = 1,
                    next_attempt_at = ?, retry_count = 0,
                    last_error_category = '', last_error_message = '',
                    reply_change_pending = 1
                WHERE task_id = ?
                """,
                (int(now), int(existing_task["task_id"])),
            )
        return 0

    def calculate_reply_safety_interval_seconds(self) -> int:
        """Periodic safety is disabled; kept for diagnostics compatibility."""
        return 0

    def activate_due_safety_scans(self, now: int) -> int:
        """Backward-compatible alias for gap-driven reply activation."""
        return self.activate_reply_gaps(now)

    def activate_reply_gaps(self, now: int) -> int:
        """Schedule dormant reply tasks whose known rcount exceeds observed replies."""
        with self._connection:
            rows = self._connection.execute(
                """
                SELECT scan_task.task_id
                FROM comment_root_state
                JOIN resource_lifecycle USING(lifecycle_id)
                JOIN scan_task
                  ON scan_task.lifecycle_id = comment_root_state.lifecycle_id
                 AND scan_task.root_rpid = comment_root_state.root_rpid
                 AND scan_task.kind = 'reply'
                WHERE resource_lifecycle.retired_at = 0
                  AND scan_task.task_state = 'dormant'
                  AND comment_root_state.known_reply_count > (
                      SELECT COUNT(*)
                      FROM observed_comment
                      WHERE observed_comment.lifecycle_id = comment_root_state.lifecycle_id
                        AND observed_comment.root_rpid = comment_root_state.root_rpid
                        AND observed_comment.is_reply = 1
                  )
                ORDER BY comment_root_state.last_seen_at DESC, scan_task.task_id
                LIMIT 500
                """
            ).fetchall()
            task_ids = [int(row["task_id"]) for row in rows]
            if not task_ids:
                return 0
            placeholders = ",".join("?" for _ in task_ids)
            cursor = self._connection.execute(
                f"""
                UPDATE scan_task
                SET task_state = 'scheduled', next_attempt_at = ?,
                    page_index = 1, reply_change_pending = 1,
                    retry_count = 0,
                    last_error_category = '', last_error_message = ''
                WHERE task_id IN ({placeholders})
                """,
                (int(now), *task_ids),
            )
        return max(0, cursor.rowcount)

    @staticmethod
    def is_deleted_comment_error(
        category: str, message: str, code: str = ""
    ) -> bool:
        normalized_category = str(category or "").strip().lower()
        normalized_code = str(code or "").strip()
        normalized_message = str(message or "")
        if normalized_category in {"gone", "deleted"}:
            return True
        if normalized_code in DELETED_COMMENT_ERROR_CODES:
            return True
        return any(
            marker in normalized_message for marker in DELETED_COMMENT_ERROR_MARKERS
        )

    def mark_scan_failed(
        self,
        task_id: int,
        category: str,
        message: str,
        next_attempt_at: int,
        attempted_at: int | None = None,
        *,
        code: str = "",
    ) -> None:
        normalized_attempted_at = (
            int(attempted_at)
            if attempted_at is not None
            else max(0, int(next_attempt_at) - 1)
        )
        if self.is_deleted_comment_error(category, message, code=code):
            self.mark_scan_terminal(
                task_id,
                category="gone",
                message=str(message or "评论已删除或不存在"),
                attempted_at=normalized_attempted_at,
            )
            return
        with self._connection:
            task = self._connection.execute(
                """
                SELECT scan_task.lifecycle_id, scan_task.scan_lane,
                       resource_lifecycle.owner_uid
                FROM scan_task
                JOIN resource_lifecycle USING(lifecycle_id)
                WHERE scan_task.task_id = ?
                """,
                (int(task_id),),
            ).fetchone()
            self._connection.execute(
                """
                UPDATE scan_task
                SET retry_count = retry_count + 1,
                    last_error_category = ?,
                    last_error_message = ?,
                    next_attempt_at = ?, task_state = 'scheduled',
                    last_attempt_at = ?
                WHERE task_id = ?
                """,
                (
                    str(category),
                    str(message),
                    int(next_attempt_at),
                    normalized_attempted_at,
                    int(task_id),
                ),
            )
            if task is not None:
                self._record_scan_attempt(normalized_attempted_at)

    def mark_scan_terminal(
        self,
        task_id: int,
        category: str,
        message: str,
        attempted_at: int,
    ) -> None:
        with self._connection:
            task = self._connection.execute(
                """
                SELECT scan_task.lifecycle_id, scan_task.root_rpid
                FROM scan_task
                WHERE scan_task.task_id = ?
                """,
                (int(task_id),),
            ).fetchone()
            self._connection.execute(
                """
                UPDATE scan_task
                SET retry_count = retry_count + 1,
                    last_error_category = ?,
                    last_error_message = ?,
                    next_attempt_at = 0,
                    task_state = 'retired',
                    last_attempt_at = ?,
                    reply_change_pending = 0,
                    page_index = 1
                WHERE task_id = ?
                """,
                (
                    str(category),
                    str(message),
                    int(attempted_at),
                    int(task_id),
                ),
            )
            if task is not None:
                self._connection.execute(
                    """
                    UPDATE comment_root_state
                    SET next_safety_scan_at = 0
                    WHERE lifecycle_id = ? AND root_rpid = ?
                    """,
                    (str(task["lifecycle_id"]), str(task["root_rpid"])),
                )
                self._record_scan_attempt(int(attempted_at))

    def _record_scan_attempt(self, attempted_at: int) -> None:
        minute_started_at = max(0, int(attempted_at)) // 60 * 60
        self._connection.execute(
            """
            INSERT INTO comment_scan_minute(minute_started_at, request_count)
            VALUES(?, 1)
            ON CONFLICT(minute_started_at) DO UPDATE SET
                request_count = comment_scan_minute.request_count + 1
            """,
            (minute_started_at,),
        )

    def has_observed_root(
        self, lifecycle_id: str, root_rpids: Sequence[str]
    ) -> bool:
        normalized = tuple(
            dict.fromkeys(str(value) for value in root_rpids if str(value))
        )
        if not normalized:
            return False
        placeholders = ",".join("?" for _ in normalized)
        row = self._connection.execute(
            f"""
            SELECT 1 FROM observed_comment
            WHERE lifecycle_id = ? AND is_reply = 0
              AND rpid IN ({placeholders})
            LIMIT 1
            """,
            (str(lifecycle_id), *normalized),
        ).fetchone()
        return row is not None

    def pending_delivery_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM event_delivery WHERE state = 'pending'"
        ).fetchone()
        return int(row["count"])

    def status(self, now: int) -> CommentJournalStatus:
        lifecycle_rows = self._connection.execute(
            "SELECT state, COUNT(*) AS count FROM resource_lifecycle GROUP BY state"
        ).fetchall()
        lifecycle_counts = {
            "bootstrapping": 0,
            "active": 0,
            "retired": 0,
        }
        for row in lifecycle_rows:
            lifecycle_counts[str(row["state"])] = int(row["count"])
        incomplete_row = self._connection.execute(
            """
            SELECT COUNT(*) AS count FROM resource_lifecycle
            WHERE incomplete_reason != ''
            """
        ).fetchone()
        scan_row = self._connection.execute(
            """
            SELECT COUNT(*) AS pending,
                   COALESCE(SUM(CASE WHEN next_attempt_at <= ? THEN 1 ELSE 0 END), 0)
                       AS overdue,
                   COALESCE(SUM(CASE WHEN retry_count > 0 THEN 1 ELSE 0 END), 0)
                       AS retrying,
                   COALESCE(MIN(next_attempt_at), 0) AS oldest_due,
                   COALESCE(MAX(last_success_at), 0) AS last_success
            FROM scan_task
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE scan_task.task_state = 'scheduled'
              AND resource_lifecycle.retired_at = 0
            """,
            (int(now),),
        ).fetchone()
        lane_rows = self._connection.execute(
            """
            SELECT scan_lane, COUNT(*) AS count
            FROM scan_task
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE scan_task.task_state = 'scheduled'
              AND scan_task.next_attempt_at <= ?
              AND resource_lifecycle.retired_at = 0
            GROUP BY scan_lane
            """,
            (int(now),),
        ).fetchall()
        lane_due_counts = {"head": 0, "reply": 0, "reconcile": 0}
        for row in lane_rows:
            lane_due_counts[str(row["scan_lane"])] = int(row["count"])
        dormant_row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM scan_task
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE scan_task.kind = 'reply'
              AND scan_task.task_state = 'dormant'
              AND resource_lifecycle.retired_at = 0
            """
        ).fetchone()
        reply_continuation_row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM scan_task
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE scan_task.kind = 'reply'
              AND scan_task.task_state = 'scheduled'
              AND scan_task.page_index > 1
              AND resource_lifecycle.retired_at = 0
            """
        ).fetchone()
        reply_change_row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM scan_task
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE scan_task.kind = 'reply'
              AND scan_task.task_state = 'scheduled'
              AND scan_task.reply_change_pending = 1
              AND resource_lifecycle.retired_at = 0
            """
        ).fetchone()
        reply_retry_row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM scan_task
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE scan_task.kind = 'reply'
              AND scan_task.task_state = 'scheduled'
              AND scan_task.retry_count > 0
              AND resource_lifecycle.retired_at = 0
            """
        ).fetchone()
        baseline_row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM resource_lifecycle
            WHERE retired_at = 0 AND baseline_completed_at = 0
            """
        ).fetchone()
        head_row = self._connection.execute(
            """
            SELECT COALESCE(MIN(next_attempt_at), 0) AS oldest_due
            FROM scan_task
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE scan_lane = 'head' AND task_state = 'scheduled'
              AND resource_lifecycle.retired_at = 0
            """
        ).fetchone()
        reconciliation_row = self._connection.execute(
            """
            SELECT
                COALESCE(MAX(CASE WHEN scan_lane = 'reconcile'
                    THEN last_success_at ELSE 0 END), 0) AS root_success,
                COALESCE(MAX(CASE WHEN scan_lane = 'reply'
                    THEN last_success_at ELSE 0 END), 0) AS reply_success
            FROM scan_task
            """
        ).fetchone()
        legacy_attempt_row = self._connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN attempted_at >= ? THEN 1 ELSE 0 END), 0)
                    AS count_15m,
                COALESCE(SUM(CASE WHEN attempted_at >= ? THEN 1 ELSE 0 END), 0)
                    AS count_60m
            FROM comment_scan_attempt
            """,
            (int(now) - 15 * 60, int(now) - 60 * 60),
        ).fetchone()
        minute_attempt_row = self._connection.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN minute_started_at >= ?
                    THEN request_count ELSE 0 END), 0) AS count_15m,
                COALESCE(SUM(CASE WHEN minute_started_at >= ?
                    THEN request_count ELSE 0 END), 0) AS count_60m
            FROM comment_scan_minute
            """,
            (
                (int(now) - 15 * 60) // 60 * 60,
                (int(now) - 60 * 60) // 60 * 60,
            ),
        ).fetchone()
        owner_rows = self._connection.execute(
            """
            SELECT resource_lifecycle.owner_uid,
                   COALESCE(MAX(scan_task.last_attempt_at), 0) AS last_attempt
            FROM resource_lifecycle
            LEFT JOIN scan_task USING(lifecycle_id)
            WHERE resource_lifecycle.retired_at = 0
            GROUP BY resource_lifecycle.owner_uid
            """
        ).fetchall()
        delivery_row = self._connection.execute(
            """
            SELECT COUNT(*) AS pending,
                   COALESCE(MIN(next_attempt_at), 0) AS oldest_due
            FROM event_delivery WHERE state = 'pending'
            """
        ).fetchone()
        terminal_reply_row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM scan_task
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE scan_task.kind = 'reply'
              AND scan_task.task_state = 'retired'
              AND resource_lifecycle.retired_at = 0
            """
        ).fetchone()
        last_root_reconciliation_at = int(reconciliation_row["root_success"])
        last_reply_reconciliation_at = int(reconciliation_row["reply_success"])
        return CommentJournalStatus(
            lifecycle_counts=lifecycle_counts,
            incomplete_count=int(incomplete_row["count"]),
            pending_scan_count=int(scan_row["pending"]),
            overdue_scan_count=int(scan_row["overdue"]),
            retrying_scan_count=int(scan_row["retrying"]),
            oldest_scan_due_at=int(scan_row["oldest_due"]),
            pending_delivery_count=int(delivery_row["pending"]),
            oldest_delivery_due_at=int(delivery_row["oldest_due"]),
            last_reconciliation_at=max(
                last_root_reconciliation_at,
                last_reply_reconciliation_at,
            ),
            lane_due_counts=lane_due_counts,
            dormant_reply_count=int(dormant_row["count"]),
            reply_change_pending_count=int(reply_change_row["count"]),
            reply_continuation_count=int(reply_continuation_row["count"]),
            reply_retrying_count=int(reply_retry_row["count"]),
            baseline_pending_count=int(baseline_row["count"]),
            oldest_head_due_at=int(head_row["oldest_due"]),
            last_root_reconciliation_at=last_root_reconciliation_at,
            last_reply_reconciliation_at=last_reply_reconciliation_at,
            request_count_15m=(
                int(legacy_attempt_row["count_15m"])
                + int(minute_attempt_row["count_15m"])
            ),
            request_count_60m=(
                int(legacy_attempt_row["count_60m"])
                + int(minute_attempt_row["count_60m"])
            ),
            reply_safety_interval_seconds=0,
            owner_last_attempt_at={
                str(row["owner_uid"]): int(row["last_attempt"])
                for row in owner_rows
            },
            reply_gap_count=self._count_reply_gaps(),
            terminal_reply_count=int(terminal_reply_row["count"]),
        )

    def next_due_delivery(self, now: int) -> PendingCommentDelivery | None:
        row = self._connection.execute(
            """
            SELECT
                event_delivery.delivery_id,
                event_delivery.event_id,
                event_delivery.unified_msg_origin,
                event_delivery.attempt_count,
                observed_comment.rpid,
                observed_comment.author_uid,
                observed_comment.author_name,
                observed_comment.text,
                observed_comment.created_at,
                observed_comment.is_reply,
                observed_comment.root_rpid,
                observed_comment.parent_rpid,
                observed_comment.image_urls_json,
                observed_comment.rich_nodes_json,
                resource_lifecycle.owner_uid,
                resource_lifecycle.owner_name,
                resource_lifecycle.resource_key,
                resource_lifecycle.resource_kind,
                resource_lifecycle.oid,
                resource_lifecycle.type_value,
                resource_lifecycle.title,
                resource_lifecycle.url
            FROM event_delivery
            JOIN comment_event USING(event_id)
            JOIN observed_comment
              ON observed_comment.lifecycle_id = comment_event.lifecycle_id
             AND observed_comment.rpid = comment_event.rpid
            JOIN resource_lifecycle
              ON resource_lifecycle.lifecycle_id = comment_event.lifecycle_id
            WHERE event_delivery.state = 'pending'
              AND event_delivery.next_attempt_at <= ?
            ORDER BY event_delivery.next_attempt_at, event_delivery.delivery_id
            LIMIT 1
            """,
            (int(now),),
        ).fetchone()
        if row is None:
            return None
        raw_images = json.loads(str(row["image_urls_json"] or "[]"))
        image_urls = (
            [str(value) for value in raw_images]
            if isinstance(raw_images, list)
            else []
        )
        raw_nodes = json.loads(str(row["rich_nodes_json"] or "[]"))
        rich_nodes = [
            BilibiliRichTextNode(
                kind=str(value.get("kind", "") or ""),
                text=str(value.get("text", "") or ""),
                image_url=str(value.get("image_url", "") or ""),
                url=str(value.get("url", "") or ""),
            )
            for value in raw_nodes
            if isinstance(value, dict) and str(value.get("kind", "") or "")
        ] if isinstance(raw_nodes, list) else []
        return PendingCommentDelivery(
            delivery_id=int(row["delivery_id"]),
            event_id=int(row["event_id"]),
            unified_msg_origin=str(row["unified_msg_origin"]),
            resource=self._resource_from_row(row),
            post=BilibiliCommentPost(
                id=str(row["rpid"]),
                author_uid=str(row["author_uid"]),
                author_name=str(row["author_name"]),
                text=str(row["text"]),
                created_at=int(row["created_at"]),
                is_reply=bool(row["is_reply"]),
                root_id=str(row["root_rpid"]),
                parent_id=str(row["parent_rpid"]),
                image_urls=image_urls,
                rich_nodes=rich_nodes,
            ),
            attempt_count=int(row["attempt_count"]),
        )

    def acknowledge_delivery(self, delivery_id: int, acknowledged_at: int) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE event_delivery
                SET state = 'acknowledged', acknowledged_at = ?,
                    last_error_category = '', last_error_message = ''
                WHERE delivery_id = ? AND state = 'pending'
                """,
                (int(acknowledged_at), int(delivery_id)),
            )

    def cancel_delivery(self, delivery_id: int) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE event_delivery
                SET state = 'cancelled'
                WHERE delivery_id = ? AND state = 'pending'
                """,
                (int(delivery_id),),
            )

    def fail_delivery(
        self,
        delivery_id: int,
        category: str,
        message: str,
        next_attempt_at: int,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE event_delivery
                SET attempt_count = attempt_count + 1,
                    last_error_category = ?, last_error_message = ?,
                    next_attempt_at = ?
                WHERE delivery_id = ? AND state = 'pending'
                """,
                (str(category), str(message), int(next_attempt_at), int(delivery_id)),
            )

    def cancel_ineligible_deliveries(
        self, active_origins: Sequence[str]
    ) -> None:
        origins = tuple(dict.fromkeys(str(origin) for origin in active_origins))
        with self._connection:
            if not origins:
                self._connection.execute(
                    """
                    UPDATE event_delivery SET state = 'cancelled'
                    WHERE state = 'pending'
                    """
                )
                return
            placeholders = ",".join("?" for _ in origins)
            self._connection.execute(
                f"""
                UPDATE event_delivery SET state = 'cancelled'
                WHERE state = 'pending'
                  AND unified_msg_origin NOT IN ({placeholders})
                """,
                origins,
            )

    def pending_delivery_origins(self) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT unified_msg_origin FROM event_delivery
            WHERE state = 'pending'
            ORDER BY delivery_id
            """
        ).fetchall()
        return [str(row["unified_msg_origin"]) for row in rows]

    def purge_retired_lifecycles(self) -> int:
        return 0

    def observed_rpids(self, lifecycle_id: str) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT rpid FROM observed_comment
            WHERE lifecycle_id = ? ORDER BY CAST(rpid AS INTEGER), rpid
            """,
            (str(lifecycle_id),),
        ).fetchall()
        return [str(row["rpid"]) for row in rows]

    def _observed_reply_count(self, lifecycle_id: str, root_rpid: str) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM observed_comment
            WHERE lifecycle_id = ? AND root_rpid = ? AND is_reply = 1
            """,
            (str(lifecycle_id), str(root_rpid)),
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    def _count_reply_gaps(self) -> int:
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM comment_root_state
            JOIN resource_lifecycle USING(lifecycle_id)
            WHERE resource_lifecycle.retired_at = 0
              AND comment_root_state.known_reply_count > (
                  SELECT COUNT(*)
                  FROM observed_comment
                  WHERE observed_comment.lifecycle_id = comment_root_state.lifecycle_id
                    AND observed_comment.root_rpid = comment_root_state.root_rpid
                    AND observed_comment.is_reply = 1
              )
            """
        ).fetchone()
        return int(row["count"]) if row is not None else 0

    @staticmethod
    def _baseline_cutoff(*, resource_published_at: int, entered_at: int) -> int:
        published_at = max(0, int(resource_published_at or 0))
        if published_at > 0:
            return published_at
        return max(0, int(entered_at) - COMMENT_BASELINE_GRACE_SECONDS)

    @staticmethod
    def _resource_from_row(row: sqlite3.Row) -> BilibiliCommentResource:
        published_at = 0
        try:
            published_at = int(row["resource_published_at"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            published_at = 0
        return BilibiliCommentResource(
            key=str(row["resource_key"]),
            owner_uid=str(row["owner_uid"]),
            owner_name=str(row["owner_name"]),
            resource_kind=str(row["resource_kind"]),
            oid=int(row["oid"]),
            type_value=int(row["type_value"]),
            title=str(row["title"]),
            url=str(row["url"]),
            published_at=published_at,
        )

    @classmethod
    def _row_to_lifecycle(cls, row: sqlite3.Row) -> CommentResourceLifecycle:
        published_at = 0
        try:
            published_at = int(row["resource_published_at"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            published_at = 0
        return CommentResourceLifecycle(
            lifecycle_id=str(row["lifecycle_id"]),
            resource=cls._resource_from_row(row),
            entered_at=int(row["entered_at"]),
            retired_at=int(row["retired_at"]),
            state=str(row["state"]),
            incomplete_reason=str(row["incomplete_reason"]),
            head_ready_at=int(row["head_ready_at"]),
            baseline_completed_at=int(row["baseline_completed_at"]),
            resource_published_at=published_at,
        )
