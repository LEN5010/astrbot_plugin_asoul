from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from asoul_bilibili import BilibiliCommentPost, BilibiliCommentResource


@dataclass(frozen=True)
class CommentResourceLifecycle:
    lifecycle_id: str
    resource: BilibiliCommentResource
    entered_at: int
    retired_at: int
    state: str
    incomplete_reason: str


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


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS owner_catalog (
    owner_uid TEXT PRIMARY KEY,
    author_name TEXT NOT NULL DEFAULT '',
    last_attempt_at INTEGER NOT NULL DEFAULT 0,
    last_success_at INTEGER NOT NULL DEFAULT 0,
    last_error_category TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT ''
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
    incomplete_reason TEXT NOT NULL DEFAULT ''
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
    baseline INTEGER NOT NULL,
    observed_at INTEGER NOT NULL,
    PRIMARY KEY(lifecycle_id, rpid)
);
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
"""


class CommentJournal:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.path)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA_SQL)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def catalog_refresh_due(
        self, owner_uid: str, now: int, interval_seconds: int
    ) -> bool:
        row = self._connection.execute(
            "SELECT last_attempt_at FROM owner_catalog WHERE owner_uid = ?",
            (str(owner_uid),),
        ).fetchone()
        return row is None or int(now) - int(row["last_attempt_at"]) >= int(
            interval_seconds
        )

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
        self, owner_uid: str, category: str, message: str
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE owner_catalog
                SET last_error_category = ?, last_error_message = ?
                WHERE owner_uid = ?
                """,
                (str(category), str(message), str(owner_uid)),
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
                    last_error_message = ''
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
                    self._connection.execute(
                        """
                        UPDATE resource_lifecycle SET
                            owner_name = ?, resource_kind = ?, oid = ?,
                            type_value = ?, title = ?, url = ?
                        WHERE lifecycle_id = ?
                        """,
                        (
                            normalized_name,
                            resource.resource_kind,
                            int(resource.oid),
                            int(resource.type_value),
                            resource.title,
                            resource.url,
                            str(row["lifecycle_id"]),
                        ),
                    )
                    continue
                retired.append(self._retire_lifecycle(row, int(now)))

            for resource_key, resource in current_resources.items():
                if resource_key in active_by_key:
                    continue
                lifecycle_id = uuid.uuid4().hex
                self._connection.execute(
                    """
                    INSERT INTO resource_lifecycle(
                        lifecycle_id, owner_uid, owner_name, resource_key,
                        resource_kind, oid, type_value, title, url, entered_at,
                        retired_at, state, incomplete_reason
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'bootstrapping', '')
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
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO scan_task(
                        lifecycle_id, kind, root_rpid, cursor, page_index,
                        bootstrap_pending, next_attempt_at
                    ) VALUES(?, 'primary', '', '', 1, 1, ?)
                    """,
                    (lifecycle_id, int(now)),
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
            "DELETE FROM scan_task WHERE lifecycle_id = ?", (lifecycle_id,)
        )
        retired_row = self._connection.execute(
            "SELECT * FROM resource_lifecycle WHERE lifecycle_id = ?",
            (lifecycle_id,),
        ).fetchone()
        return self._row_to_lifecycle(retired_row)

    def next_due_scan_task(self, now: int) -> CommentScanTask | None:
        row = self._connection.execute(
            """
            SELECT
                scan_task.task_id,
                scan_task.lifecycle_id,
                scan_task.kind,
                scan_task.root_rpid,
                scan_task.cursor,
                scan_task.page_index,
                scan_task.retry_count,
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
            WHERE resource_lifecycle.retired_at = 0
              AND scan_task.next_attempt_at <= ?
            ORDER BY
              CASE
                WHEN scan_task.kind = 'primary' AND scan_task.cursor = '' THEN 0
                WHEN scan_task.kind = 'reply' THEN 1
                ELSE 2
              END,
              scan_task.next_attempt_at,
              scan_task.task_id
            LIMIT 1
            """,
            (int(now),),
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
        )

    def commit_scan_page(
        self,
        task: CommentScanTask,
        posts: Sequence[BilibiliCommentPost],
        target_uids: Sequence[str],
        target_origins: Sequence[str],
        now: int,
        *,
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
            for post in posts:
                baseline = int(int(post.created_at) < entered_at)
                inserted = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO observed_comment(
                        lifecycle_id, rpid, author_uid, author_name, text,
                        created_at, is_reply, root_rpid, parent_rpid,
                        image_urls_json, baseline, observed_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        baseline,
                        int(now),
                    ),
                )
                if (
                    inserted.rowcount == 1
                    and not baseline
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

                if not post.is_reply:
                    reply_task = self._connection.execute(
                        """
                        INSERT OR IGNORE INTO scan_task(
                            lifecycle_id, kind, root_rpid, cursor, page_index,
                            bootstrap_pending, next_attempt_at
                        ) VALUES(?, 'reply', ?, '', 1, ?, ?)
                        """,
                        (
                            str(task.lifecycle_id),
                            str(post.id),
                            int(lifecycle_state == "bootstrapping"),
                            int(now),
                        ),
                    )
                    roots_enqueued += max(0, reply_task.rowcount)

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
                        last_error_category = '', last_error_message = ''
                    WHERE task_id = ?
                    """,
                    (
                        "" if stream_exhausted else str(next_cursor),
                        int(stream_exhausted),
                        int(next_sweep_at),
                        int(stream_exhausted),
                        int(now),
                        int(task.task_id),
                    ),
                )
            else:
                self._connection.execute(
                    """
                    UPDATE scan_task SET
                        cursor = '', page_index = ?,
                        bootstrap_pending = CASE WHEN ? THEN 0 ELSE bootstrap_pending END,
                        next_attempt_at = ?, retry_count = 0,
                        last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END,
                        last_error_category = '', last_error_message = ''
                    WHERE task_id = ?
                    """,
                    (
                        1 if stream_exhausted else int(next_page_index),
                        int(stream_exhausted),
                        int(next_sweep_at),
                        int(stream_exhausted),
                        int(now),
                        int(task.task_id),
                    ),
                )

            if lifecycle_state == "bootstrapping":
                pending = self._connection.execute(
                    """
                    SELECT 1 FROM scan_task
                    WHERE lifecycle_id = ? AND bootstrap_pending = 1
                    LIMIT 1
                    """,
                    (str(task.lifecycle_id),),
                ).fetchone()
                if pending is None:
                    self._connection.execute(
                        """
                        UPDATE resource_lifecycle SET state = 'active'
                        WHERE lifecycle_id = ? AND state = 'bootstrapping'
                        """,
                        (str(task.lifecycle_id),),
                    )
                    lifecycle_activated = True

        return PageCommitResult(
            events_created=events_created,
            deliveries_created=deliveries_created,
            roots_enqueued=roots_enqueued,
            lifecycle_activated=lifecycle_activated,
        )

    def mark_scan_failed(
        self,
        task_id: int,
        category: str,
        message: str,
        next_attempt_at: int,
    ) -> None:
        with self._connection:
            self._connection.execute(
                """
                UPDATE scan_task
                SET retry_count = retry_count + 1,
                    last_error_category = ?,
                    last_error_message = ?,
                    next_attempt_at = ?
                WHERE task_id = ?
                """,
                (str(category), str(message), int(next_attempt_at), int(task_id)),
            )

    def pending_delivery_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM event_delivery WHERE state = 'pending'"
        ).fetchone()
        return int(row["count"])

    def observed_rpids(self, lifecycle_id: str) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT rpid FROM observed_comment
            WHERE lifecycle_id = ? ORDER BY CAST(rpid AS INTEGER), rpid
            """,
            (str(lifecycle_id),),
        ).fetchall()
        return [str(row["rpid"]) for row in rows]

    @staticmethod
    def _resource_from_row(row: sqlite3.Row) -> BilibiliCommentResource:
        return BilibiliCommentResource(
            key=str(row["resource_key"]),
            owner_uid=str(row["owner_uid"]),
            owner_name=str(row["owner_name"]),
            resource_kind=str(row["resource_kind"]),
            oid=int(row["oid"]),
            type_value=int(row["type_value"]),
            title=str(row["title"]),
            url=str(row["url"]),
        )

    @classmethod
    def _row_to_lifecycle(cls, row: sqlite3.Row) -> CommentResourceLifecycle:
        return CommentResourceLifecycle(
            lifecycle_id=str(row["lifecycle_id"]),
            resource=cls._resource_from_row(row),
            entered_at=int(row["entered_at"]),
            retired_at=int(row["retired_at"]),
            state=str(row["state"]),
            incomplete_reason=str(row["incomplete_reason"]),
        )
