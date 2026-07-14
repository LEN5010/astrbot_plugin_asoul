# Accurate Comment Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the bounded comment-window poller with a durable crawler and per-group outbox that captures target comments from the active three dynamics and three videos with at-least-once delivery.

**Architecture:** Keep Bilibili parsing in `BilibiliGateway`, place all SQLite transactions in a new `CommentJournal`, and orchestrate page-sized scan and delivery work in a new `CommentCaptureCoordinator`. `BilibiliRuntime` owns one coordinator, refreshes the six-resource catalogs, serializes comment API requests through the existing gateway limiter, and converts due outbox rows into AstrBot messages.

**Tech Stack:** Python 3.10+, `asyncio`, standard-library `sqlite3`, AstrBot `StarTools`, `bilibili-api-python==17.4.1`, and `unittest`.

## Global Constraints

- Monitor exactly the newest three non-video dynamics and newest three videos per configured owner UID.
- Do not notify comments with `ctime < resource_lifecycle.entered_at`; treat equal-second comments as new.
- Stop creating scan work when a resource leaves the active 3+3 catalog; retain already captured deliveries.
- Identify an observation by `(lifecycle_id, rpid)`, never by `ctime` or text.
- Snapshot target UIDs and currently eligible group origins when an event is first captured; do not replay observations after configuration changes.
- Mark a group delivery acknowledged only after `send_message` succeeds and the SQLite acknowledgement commits.
- Preserve the two-second global comment-request spacing and cap retry delays at 43,200 seconds.
- Never log, persist in the repository, or print Bilibili cookies or authorization headers.
- Pin `bilibili-api-python==17.4.1`; do not add a new runtime dependency for persistence or scheduling.
- Keep dynamic, video, live, calendar, and rendering behavior unchanged.

---

## File Structure

- Create `asoul_comment_journal.py`: SQLite schema, lifecycle/catalog synchronization, atomic page commits, scan retries, delivery acknowledgements, cleanup, and status queries.
- Create `asoul_comment_capture.py`: scan retry policy, gateway orchestration, notification construction, and one-item delivery execution.
- Modify `asoul_bilibili.py`: public page-result models and one-page root/reply gateway methods; retain general Bilibili parsing.
- Modify `asoul_bilibili_runtime.py`: own the journal/coordinator, refresh catalogs, run page-sized work, send outbox deliveries, and render status.
- Modify `main.py`: resolve the AstrBot plugin data directory and pass the database path into the runtime.
- Modify `test_asoul_push_targets.py`: add a `StarTools` stub with an isolated temporary data directory.
- Create `test_comment_journal.py`: persistence, lifecycle, transaction, retirement, and status tests.
- Create `test_comment_capture.py`: primary/reply pagination, retry, reconciliation, and delivery tests.
- Modify `test_bilibili_monitor.py`: page-adapter tests and removal of assertions for the retired fixed-window methods.
- Modify `test_bilibili_runtime_diagnostics.py` and `test_asoul_delivery_confirmation.py`: runtime scheduler, status, and per-group acknowledgement tests.
- Create `tools/verify_bilibili_comments.py`: credential-safe, read-only real API harness with a recording sink.
- Create `test_bilibili_integration_harness.py`: credential-path, redaction, and report tests without network access.
- Modify `_conf_schema.json`, `README.md`, and `requirements.txt`: document accurate-capture behavior and pin the verified Bilibili library.

---

### Task 1: Add One-Page Bilibili Comment Adapters

**Files:**
- Modify: `asoul_bilibili.py:138-149,611-842`
- Modify: `test_bilibili_monitor.py:1314-1519`
- Modify: `requirements.txt:1-3`

**Interfaces:**
- Produces: `BilibiliRootCommentPage(posts: list[BilibiliCommentPost], next_offset: str)`.
- Produces: `BilibiliReplyCommentPage(posts: list[BilibiliCommentPost], next_page_index: int)`.
- Produces: `BilibiliGateway.get_root_comment_page(resource, offset="")`.
- Produces: `BilibiliGateway.get_reply_comment_page(resource, root_id, page_index)`.

- [ ] **Step 1: Write failing gateway page tests**

Add these tests to `BilibiliParsingTest` in `test_bilibili_monitor.py`:

```python
def test_root_comment_page_returns_cursor_without_truncating_roots(self) -> None:
    replies = [
        {
            "rpid_str": str(20_000 - index),
            "ctime": 20_000 - index,
            "parent": 0,
            "member": {"mid": "100", "uname": "测试账号"},
            "content": {"message": f"第 {index} 条"},
        }
        for index in range(25)
    ]
    self.gateway.comment_module = FakeCommentModule(
        {
            "": {
                "replies": replies,
                "cursor": {"pagination_reply": {"next_offset": "page-2"}},
            }
        }
    )

    page = asyncio.run(
        self.gateway.get_root_comment_page(self._comment_resource(), offset="")
    )

    self.assertEqual(len([post for post in page.posts if not post.is_reply]), 25)
    self.assertEqual(page.next_offset, "page-2")
    self.assertEqual(self.gateway.comment_module.calls, [""])

def test_reply_comment_page_does_not_assume_newest_first(self) -> None:
    self.gateway.comment_module = FakeCommentModule({})
    self.gateway.comment_module.sub_comment_pages = {
        ("9001", 1): {
            "replies": [
                {
                    "rpid_str": "9002",
                    "ctime": 102,
                    "parent": 9001,
                    "root": 9001,
                    "member": {"mid": "100", "uname": "测试账号"},
                    "content": {"message": "较早回复"},
                },
                {
                    "rpid_str": "9004",
                    "ctime": 104,
                    "parent": 9001,
                    "root": 9001,
                    "member": {"mid": "100", "uname": "测试账号"},
                    "content": {"message": "较晚回复"},
                },
            ]
        }
    }

    page = asyncio.run(
        self.gateway.get_reply_comment_page(
            self._comment_resource(), root_id="9001", page_index=1
        )
    )

    self.assertEqual([post.id for post in page.posts], ["9002", "9004"])
    self.assertEqual(page.next_page_index, 0)

def test_root_comment_page_rejects_reply_without_rpid(self) -> None:
    self.gateway.comment_module = FakeCommentModule(
        {
            "": {
                "replies": [
                    {
                        "ctime": 104,
                        "parent": 0,
                        "member": {"mid": "100", "uname": "测试账号"},
                        "content": {"message": "缺少 rpid"},
                    }
                ]
            }
        }
    )

    with self.assertRaisesRegex(BilibiliCommentPayloadError, "rpid"):
        asyncio.run(self.gateway.get_root_comment_page(self._comment_resource()))
```

Add `BilibiliCommentPayloadError` to the existing imports and add this helper to the same class so later tests use one resource definition:

```python
def _comment_resource(self) -> BilibiliCommentResource:
    return BilibiliCommentResource(
        key="video:2003",
        owner_uid="100",
        owner_name="测试账号",
        resource_kind="video",
        oid=2003,
        type_value=1,
        title="第三个视频",
        url="https://www.bilibili.com/video/BV3",
    )
```

- [ ] **Step 2: Run the tests and verify the missing API failure**

Run:

```bash
python3 -m unittest \
  test_bilibili_monitor.BilibiliParsingTest.test_root_comment_page_returns_cursor_without_truncating_roots \
  test_bilibili_monitor.BilibiliParsingTest.test_reply_comment_page_does_not_assume_newest_first \
  test_bilibili_monitor.BilibiliParsingTest.test_root_comment_page_rejects_reply_without_rpid
```

Expected: `ERROR` with `ImportError` for `BilibiliCommentPayloadError`; after adding that record alone, the tests fail with `AttributeError` for the two page methods.

- [ ] **Step 3: Add the page models and gateway methods**

Add next to `BilibiliCommentPost` in `asoul_bilibili.py`:

```python
class BilibiliCommentPayloadError(ValueError):
    """The comments endpoint returned data that cannot advance safely."""


@dataclass(frozen=True)
class BilibiliRootCommentPage:
    posts: List[BilibiliCommentPost] = field(default_factory=list)
    next_offset: str = ""


@dataclass(frozen=True)
class BilibiliReplyCommentPage:
    posts: List[BilibiliCommentPost] = field(default_factory=list)
    next_page_index: int = 0
```

Add these methods to `BilibiliGateway` and keep `_execute_comment_request` as the only route to the library:

```python
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
    seen_ids: set[str] = set()

    def append_reply(raw_reply: Dict[str, Any], root_id: str = "") -> None:
        post = self._parse_comment_post(raw_reply, root_id=root_id)
        if post is None:
            raise BilibiliCommentPayloadError("comment reply is missing rpid")
        if post.id in seen_ids:
            return
        seen_ids.add(post.id)
        posts.append(post)
        nested = raw_reply.get("replies")
        if not isinstance(nested, list):
            return
        nested_root_id = post.root_id or post.id
        for raw_nested in nested:
            if isinstance(raw_nested, dict):
                append_reply(raw_nested, nested_root_id)

    replies = payload.get("replies")
    if replies is not None and not isinstance(replies, list):
        raise BilibiliCommentPayloadError("root replies must be a list or null")
    for raw_reply in replies or []:
        if not isinstance(raw_reply, dict):
            raise BilibiliCommentPayloadError("root reply must be a dict")
        append_reply(raw_reply)
    return BilibiliRootCommentPage(
        posts=posts,
        next_offset=self._extract_comment_next_offset(payload),
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
```

Change `requirements.txt` to:

```text
aiohttp
bilibili-api-python==17.4.1
Pillow
```

- [ ] **Step 4: Run gateway and full regression tests**

Run:

```bash
python3 -m unittest test_bilibili_monitor
python3 -m unittest
```

Expected: both commands end with `OK`.

- [ ] **Step 5: Commit the page adapter**

```bash
git add asoul_bilibili.py test_bilibili_monitor.py requirements.txt
git commit -m "feat: expose paged Bilibili comment reads"
```

---

### Task 2: Add the SQLite Journal and Resource Lifecycles

**Files:**
- Create: `asoul_comment_journal.py`
- Create: `test_comment_journal.py`

**Interfaces:**
- Consumes: `BilibiliCommentResource` from Task 1.
- Produces: `CommentResourceLifecycle`, `CommentScanTask`, `CatalogSyncResult`, and `CommentJournal`.
- Produces: `CommentJournal.catalog_refresh_due`, `begin_catalog_refresh`, `sync_resource_catalog`, `fail_catalog_refresh`, `retire_unconfigured_owners`, `next_due_scan_task`, and `close`.

- [ ] **Step 1: Write failing lifecycle persistence tests**

Create `test_comment_journal.py` with:

```python
import tempfile
import unittest
from pathlib import Path

from asoul_bilibili import BilibiliCommentResource
from asoul_comment_journal import CommentJournal


def video_resource(oid: int) -> BilibiliCommentResource:
    return BilibiliCommentResource(
        key=f"video:{oid}",
        owner_uid="100",
        owner_name="测试账号",
        resource_kind="video",
        oid=oid,
        type_value=1,
        title=f"视频 {oid}",
        url=f"https://www.bilibili.com/video/{oid}",
    )


class CommentJournalLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "comments.sqlite3"
        self.journal = CommentJournal(self.db_path)

    def tearDown(self) -> None:
        self.journal.close()
        self.temp_dir.cleanup()

    def test_catalog_sync_creates_one_lifecycle_and_primary_task(self) -> None:
        result = self.journal.sync_resource_catalog(
            owner_uid="100",
            author_name="测试账号",
            resources=[video_resource(2003)],
            now=1_700_000_000,
        )

        self.assertEqual(len(result.activated), 1)
        lifecycle = result.activated[0]
        self.assertEqual(lifecycle.entered_at, 1_700_000_000)
        self.assertEqual(lifecycle.state, "bootstrapping")
        task = self.journal.next_due_scan_task(1_700_000_000)
        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual(task.kind, "primary")
        self.assertEqual(task.cursor, "")

    def test_retired_resource_reappears_as_a_new_lifecycle(self) -> None:
        first = self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        ).activated[0]
        retired = self.journal.sync_resource_catalog(
            "100", "测试账号", [], 200
        ).retired[0]
        second = self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 300
        ).activated[0]

        self.assertEqual(retired.lifecycle_id, first.lifecycle_id)
        self.assertNotEqual(second.lifecycle_id, first.lifecycle_id)
        self.assertEqual(second.entered_at, 300)

    def test_catalog_attempt_survives_restart(self) -> None:
        self.journal.begin_catalog_refresh("100", now=100)
        self.journal.close()
        self.journal = CommentJournal(self.db_path)

        self.assertFalse(
            self.journal.catalog_refresh_due("100", now=699, interval_seconds=600)
        )
        self.assertTrue(
            self.journal.catalog_refresh_due("100", now=700, interval_seconds=600)
        )
```

- [ ] **Step 2: Run the new test module and verify import failure**

Run:

```bash
python3 -m unittest test_comment_journal
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'asoul_comment_journal'`.

- [ ] **Step 3: Create the journal models and schema**

Create `asoul_comment_journal.py` with these public records and schema. Keep all SQL in this module:

```python
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
```

Implement `CommentJournal.__init__` and `close` as:

```python
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
```

Implement catalog cooldown with persisted attempt time:

```python
def catalog_refresh_due(
    self, owner_uid: str, now: int, interval_seconds: int
) -> bool:
    row = self._connection.execute(
        "SELECT last_attempt_at FROM owner_catalog WHERE owner_uid = ?",
        (str(owner_uid),),
    ).fetchone()
    return row is None or int(now) - int(row["last_attempt_at"]) >= int(interval_seconds)

def begin_catalog_refresh(self, owner_uid: str, now: int) -> None:
    with self._connection:
        self._connection.execute(
            """
            INSERT INTO owner_catalog(owner_uid, last_attempt_at)
            VALUES(?, ?)
            ON CONFLICT(owner_uid) DO UPDATE SET last_attempt_at = excluded.last_attempt_at
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
```

Implement `sync_resource_catalog` as one transaction: update `owner_catalog`, retire missing active resource rows, delete their scan tasks, insert one lifecycle and one primary task for every newly active resource, and return immutable lifecycle records. Generate IDs with `uuid.uuid4().hex`. A retirement is incomplete when any task has `retry_count > 0`, a non-empty primary cursor, or a reply `page_index > 1`; store `"retired with unfinished scan"` in `incomplete_reason` in that case.

Implement `next_due_scan_task(now)` with a join from `scan_task` to `resource_lifecycle`, restricted to non-retired lifecycles and ordered by:

```sql
ORDER BY
  CASE
    WHEN scan_task.kind = 'primary' AND scan_task.cursor = '' THEN 0
    WHEN scan_task.kind = 'reply' THEN 1
    ELSE 2
  END,
  scan_task.next_attempt_at,
  scan_task.task_id
LIMIT 1
```

Map the joined row into the exact `CommentScanTask` signature above. Add `retire_unconfigured_owners(active_owner_uids, now)` using the same retirement transaction for owner UIDs no longer configured.

- [ ] **Step 4: Run lifecycle tests and inspect the database-free regression suite**

Run:

```bash
python3 -m unittest test_comment_journal.CommentJournalLifecycleTest
python3 -m unittest
```

Expected: both commands end with `OK`.

- [ ] **Step 5: Commit the journal foundation**

```bash
git add asoul_comment_journal.py test_comment_journal.py
git commit -m "feat: add durable comment resource journal"
```

---

### Task 3: Commit Observations and Outbox Rows Atomically

**Files:**
- Modify: `asoul_comment_journal.py`
- Modify: `test_comment_journal.py`

**Interfaces:**
- Consumes: `CommentScanTask`, `BilibiliCommentPost`, and active target UID/origin snapshots.
- Produces: `PageCommitResult(events_created, deliveries_created, roots_enqueued, lifecycle_activated)`.
- Produces: `CommentJournal.commit_scan_page`, `mark_scan_failed`, `pending_delivery_count`, and `observed_rpids`.

- [ ] **Step 1: Write failing atomic page-commit tests**

Add to `test_comment_journal.py`:

```python
from asoul_bilibili import BilibiliCommentPost


class CommentJournalPageCommitTest(CommentJournalLifecycleTest):
    def _activate_resource(self, entered_at: int = 100):
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], entered_at
        )
        task = self.journal.next_due_scan_task(entered_at)
        assert task is not None
        return task

    def test_page_commit_suppresses_history_and_enqueues_same_second(self) -> None:
        task = self._activate_resource(entered_at=100)
        posts = [
            BilibiliCommentPost(
                id="9001", author_uid="100", author_name="测试账号",
                text="历史评论", created_at=99, is_reply=False, root_id="9001",
            ),
            BilibiliCommentPost(
                id="9002", author_uid="100", author_name="测试账号",
                text="边界评论", created_at=100, is_reply=False, root_id="9002",
            ),
        ]

        result = self.journal.commit_scan_page(
            task=task,
            posts=posts,
            target_uids=["100"],
            target_origins=["aiocqhttp:GroupMessage:1"],
            now=101,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )

        self.assertEqual(result.events_created, 1)
        self.assertEqual(result.deliveries_created, 1)
        self.assertEqual(self.journal.observed_rpids(task.lifecycle_id), ["9001", "9002"])

    def test_duplicate_page_is_idempotent(self) -> None:
        task = self._activate_resource()
        post = BilibiliCommentPost(
            id="9002", author_uid="100", author_name="测试账号",
            text="新评论", created_at=101, is_reply=False, root_id="9002",
        )
        arguments = dict(
            task=task, posts=[post], target_uids=["100"],
            target_origins=["origin-a", "origin-b"], now=102,
            next_cursor="page-2", next_page_index=0, next_sweep_at=102,
        )
        first = self.journal.commit_scan_page(**arguments)
        second = self.journal.commit_scan_page(**arguments)

        self.assertEqual(first.events_created, 1)
        self.assertEqual(second.events_created, 0)
        self.assertEqual(self.journal.pending_delivery_count(), 2)

    def test_invalid_ctime_rolls_back_observation_and_cursor(self) -> None:
        task = self._activate_resource()
        post = BilibiliCommentPost(
            id="9002", author_uid="100", author_name="测试账号",
            text="无时间", created_at=0, is_reply=False, root_id="9002",
        )

        with self.assertRaisesRegex(ValueError, "valid ctime"):
            self.journal.commit_scan_page(
                task, [post], ["100"], ["origin-a"], 102,
                next_cursor="page-2", next_page_index=0, next_sweep_at=102,
            )

        reloaded = self.journal.next_due_scan_task(102)
        assert reloaded is not None
        self.assertEqual(reloaded.cursor, "")
        self.assertEqual(self.journal.observed_rpids(task.lifecycle_id), [])
```

- [ ] **Step 2: Run the page-commit tests and verify missing method failures**

Run:

```bash
python3 -m unittest test_comment_journal.CommentJournalPageCommitTest
```

Expected: `ERROR` for the missing `commit_scan_page` method.

- [ ] **Step 3: Add atomic commit records and methods**

Add:

```python
@dataclass(frozen=True)
class PageCommitResult:
    events_created: int
    deliveries_created: int
    roots_enqueued: int
    lifecycle_activated: bool
```

Implement `commit_scan_page` with this exact signature:

```python
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
```

Within one `with self._connection:` transaction:

1. Reload the lifecycle and reject a retired lifecycle.
2. Validate every post has a non-empty ID and `created_at > 0` before writing anything.
3. `INSERT OR IGNORE` each observation, storing `baseline = int(post.created_at < entered_at)` and JSON-encoded image URLs.
4. Only when the observation insert changed one row, the post is not baseline, and `author_uid` is in the captured target UID set, insert one `comment_event` and one pending `event_delivery` per captured origin.
5. For each non-reply post, `INSERT OR IGNORE` a reply task at page 1. Set its `bootstrap_pending` to 1 only while the lifecycle is bootstrapping.
6. Update the current task to the returned cursor/page. When its page stream is exhausted, reset its cursor/page to the start, set `bootstrap_pending = 0`, set `last_success_at = now`, clear retry/error fields, and set `next_attempt_at = next_sweep_at`.
7. Activate a bootstrapping lifecycle only when no task for it has `bootstrap_pending = 1`.

Use `cursor` only for primary tasks and `page_index` only for reply tasks. Return counts from SQLite `rowcount` values; query the lifecycle after the activation update to populate `lifecycle_activated`.

Add these exact support methods:

```python
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
```

- [ ] **Step 4: Run journal and full tests**

Run:

```bash
python3 -m unittest test_comment_journal
python3 -m unittest
```

Expected: both commands end with `OK`.

- [ ] **Step 5: Commit atomic observation storage**

```bash
git add asoul_comment_journal.py test_comment_journal.py
git commit -m "feat: persist comment observations and outbox rows"
```

---

### Task 4: Add the Recoverable Capture Coordinator

**Files:**
- Create: `asoul_comment_capture.py`
- Create: `test_comment_capture.py`
- Modify: `asoul_comment_journal.py`

**Interfaces:**
- Consumes: Task 1 page adapters and Task 3 journal transactions.
- Produces: `CommentRetryPolicy.delay_seconds(retry_count)`.
- Produces: `CommentCaptureCoordinator.run_scan_task(task, target_uids, target_origins, now)`.
- Produces: constants `COMMENT_PRIMARY_RESCAN_SECONDS=180`, `COMMENT_REPLY_RESCAN_SECONDS=1800`, and `COMMENT_MAX_RETRY_SECONDS=43200`.

- [ ] **Step 1: Write failing primary, reply, and retry tests**

Create `test_comment_capture.py` with these imports, record constructors, and fake gateway:

```python
import tempfile
import unittest
from pathlib import Path

from asoul_bilibili import (
    BilibiliCommentPost,
    BilibiliReplyCommentPage,
    BilibiliRootCommentPage,
)
from asoul_comment_capture import (
    CommentCaptureCoordinator,
    CommentCaptureError,
    CommentRetryPolicy,
)
from asoul_comment_journal import CommentJournal
from test_comment_journal import video_resource


def comment_post(rpid: str, created_at: int) -> BilibiliCommentPost:
    return BilibiliCommentPost(
        id=str(rpid),
        author_uid="100",
        author_name="测试账号",
        text=f"评论 {rpid}",
        created_at=int(created_at),
        is_reply=False,
        root_id=str(rpid),
    )


def root_post(rpid: str, created_at: int) -> BilibiliCommentPost:
    return BilibiliCommentPost(
        id=str(rpid),
        author_uid="100",
        author_name="测试账号",
        text=f"一级评论 {rpid}",
        created_at=int(created_at),
        is_reply=False,
        root_id=str(rpid),
        reply_count=1,
    )


def reply_post(
    rpid: str, created_at: int, root_rpid: str
) -> BilibiliCommentPost:
    return BilibiliCommentPost(
        id=str(rpid),
        author_uid="100",
        author_name="测试账号",
        text=f"楼中楼 {rpid}",
        created_at=int(created_at),
        is_reply=True,
        root_id=str(root_rpid),
        parent_id=str(root_rpid),
    )


class FakePagedGateway:
    def __init__(self) -> None:
        self.root_pages: dict[str, BilibiliRootCommentPage] = {}
        self.reply_pages: dict[tuple[str, int], BilibiliReplyCommentPage] = {}
        self.root_offsets: list[str] = []
        self.reply_page_indexes: list[int] = []
        self.root_error: Exception | None = None
        self.reply_error: Exception | None = None

    async def get_root_comment_page(self, resource, offset=""):
        self.root_offsets.append(str(offset))
        if self.root_error is not None:
            raise self.root_error
        return self.root_pages.get(str(offset), BilibiliRootCommentPage())

    async def get_reply_comment_page(
        self, resource, root_id: str, page_index: int
    ):
        self.reply_page_indexes.append(int(page_index))
        if self.reply_error is not None:
            raise self.reply_error
        return self.reply_pages.get(
            (str(root_id), int(page_index)),
            BilibiliReplyCommentPage(),
        )


class CommentCaptureCoordinatorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.journal = CommentJournal(
            Path(self.temp_dir.name) / "comments.sqlite3"
        )
        self.gateway = FakePagedGateway()
        self.coordinator = CommentCaptureCoordinator(
            gateway=self.gateway,
            journal=self.journal,
            classify_error=lambda exc: CommentCaptureError(
                category="risk_control", code="412", message="请求被拒绝"
            ),
            retry_policy=CommentRetryPolicy(random_value=lambda: 0.5),
        )
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], now=100
        )

    async def asyncTearDown(self) -> None:
        self.journal.close()
        self.temp_dir.cleanup()

    async def test_primary_cursor_resumes_until_exhausted(self) -> None:
        self.gateway.root_pages = {
            "": BilibiliRootCommentPage(
                posts=[comment_post("9003", 103)], next_offset="page-2"
            ),
            "page-2": BilibiliRootCommentPage(
                posts=[comment_post("9002", 102)], next_offset=""
            ),
        }
        first = self.journal.next_due_scan_task(100)
        assert first is not None
        await self.coordinator.run_scan_task(first, ["100"], ["origin-a"], 103)

        second = self.journal.next_due_scan_task(103)
        assert second is not None
        self.assertEqual(second.cursor, "page-2")
        await self.coordinator.run_scan_task(second, ["100"], ["origin-a"], 104)

        self.assertEqual(self.gateway.root_offsets, ["", "page-2"])
        self.assertEqual(self.journal.observed_rpids(first.lifecycle_id), ["9002", "9003"])

    async def test_reply_scan_continues_past_three_pages(self) -> None:
        self.gateway.root_pages = {
            "": BilibiliRootCommentPage(
                posts=[root_post("9001", 101)], next_offset=""
            )
        }
        primary = self.journal.next_due_scan_task(100)
        assert primary is not None
        await self.coordinator.run_scan_task(primary, ["100"], ["origin-a"], 101)
        self.gateway.reply_pages = {
            ("9001", page): BilibiliReplyCommentPage(
                posts=[reply_post(str(9100 + page), 101 + page, "9001")],
                next_page_index=page + 1 if page < 4 else 0,
            )
            for page in range(1, 5)
        }

        for now in range(102, 106):
            task = self.journal.next_due_scan_task(now)
            assert task is not None
            await self.coordinator.run_scan_task(task, ["100"], ["origin-a"], now)

        self.assertEqual(self.gateway.reply_page_indexes, [1, 2, 3, 4])

    async def test_412_preserves_cursor_and_uses_capped_backoff(self) -> None:
        self.gateway.root_error = RuntimeError("HTTP 412")
        task = self.journal.next_due_scan_task(100)
        assert task is not None

        await self.coordinator.run_scan_task(task, ["100"], ["origin-a"], 100)

        retried = self.journal.next_due_scan_task(160)
        assert retried is not None
        self.assertEqual(retried.cursor, "")
        self.assertEqual(retried.retry_count, 1)

    async def test_completed_root_is_revisited_for_late_reply(self) -> None:
        self.gateway.root_pages = {
            "": BilibiliRootCommentPage(
                posts=[root_post("9001", 101)], next_offset=""
            )
        }
        primary = self.journal.next_due_scan_task(100)
        assert primary is not None
        await self.coordinator.run_scan_task(primary, ["100"], ["origin-a"], 101)
        first_reply_scan = self.journal.next_due_scan_task(101)
        assert first_reply_scan is not None
        await self.coordinator.run_scan_task(
            first_reply_scan, ["100"], ["origin-a"], 102
        )

        head_rescan = self.journal.next_due_scan_task(1_902)
        assert head_rescan is not None
        await self.coordinator.run_scan_task(
            head_rescan, ["100"], ["origin-a"], 1_902
        )
        self.gateway.reply_pages[("9001", 1)] = BilibiliReplyCommentPage(
            posts=[reply_post("9002", 1_901, "9001")],
            next_page_index=0,
        )
        late_reply_scan = self.journal.next_due_scan_task(1_902)
        assert late_reply_scan is not None
        await self.coordinator.run_scan_task(
            late_reply_scan, ["100"], ["origin-a"], 1_902
        )

        self.assertIn("9002", self.journal.observed_rpids(primary.lifecycle_id))

    def test_retry_delay_is_capped_at_twelve_hours(self) -> None:
        policy = CommentRetryPolicy(random_value=lambda: 0.5)

        self.assertEqual(policy.delay_seconds(20), 43_200)
```

- [ ] **Step 2: Run the coordinator tests and verify missing module failure**

Run:

```bash
python3 -m unittest test_comment_capture.CommentCaptureCoordinatorTest
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'asoul_comment_capture'`.

- [ ] **Step 3: Implement retry policy and one-task scan execution**

Create `asoul_comment_capture.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol, Sequence

from asoul_bilibili import BilibiliGateway
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
    def __call__(self, exc: Exception) -> CommentCaptureError:
        raise NotImplementedError


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
        return min(self._cap_seconds, max(self._base_seconds, int(raw * jitter)))


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
                        now if page.next_offset else now + COMMENT_PRIMARY_RESCAN_SECONDS
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
```

The protocol method is static typing only; do not leave any runtime method unimplemented. Reset scan retries inside `commit_scan_page` by clearing retry and error columns on every successful page.

- [ ] **Step 4: Run coordinator, journal, and full tests**

Run:

```bash
python3 -m unittest test_comment_capture test_comment_journal
python3 -m unittest
```

Expected: both commands end with `OK`.

- [ ] **Step 5: Commit the recoverable scanner**

```bash
git add asoul_comment_capture.py asoul_comment_journal.py test_comment_capture.py
git commit -m "feat: add recoverable comment page scheduler"
```

---

### Task 5: Add Per-Group At-Least-Once Delivery

**Files:**
- Modify: `asoul_comment_journal.py`
- Modify: `asoul_comment_capture.py`
- Modify: `test_comment_capture.py`

**Interfaces:**
- Produces: `PendingCommentDelivery` containing the resource, post, target origin, and attempt count.
- Produces: `CommentJournal.next_due_delivery`, `acknowledge_delivery`, `fail_delivery`, `cancel_ineligible_deliveries`, and `purge_retired_lifecycles`.
- Produces: `CommentCaptureCoordinator.deliver_one(send, now) -> bool`.

- [ ] **Step 1: Write failing independent-delivery tests**

Add to `test_comment_capture.py`:

```python
class CommentDeliveryTest(CommentCaptureCoordinatorTest):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.gateway.root_pages = {
            "": BilibiliRootCommentPage(
                posts=[comment_post("9002", 101)], next_offset=""
            )
        }
        task = self.journal.next_due_scan_task(100)
        assert task is not None
        await self.coordinator.run_scan_task(
            task, ["100"], ["origin-ok", "origin-retry"], 101
        )

    async def test_groups_acknowledge_independently(self) -> None:
        attempts: dict[str, int] = {}

        async def sender(origin, notification) -> None:
            attempts[origin] = attempts.get(origin, 0) + 1
            if origin == "origin-retry" and attempts[origin] == 1:
                raise RuntimeError("send failed")

        await self.coordinator.deliver_one(sender, now=101)
        await self.coordinator.deliver_one(sender, now=101)
        await self.coordinator.deliver_one(sender, now=161)

        self.assertEqual(attempts, {"origin-ok": 1, "origin-retry": 2})
        self.assertEqual(self.journal.pending_delivery_count(), 0)

    async def test_ack_persistence_failure_leaves_delivery_pending(self) -> None:
        original_ack = self.journal.acknowledge_delivery
        calls = 0

        def fail_first_ack(delivery_id: int, acknowledged_at: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("database unavailable")
            original_ack(delivery_id, acknowledged_at)

        self.journal.acknowledge_delivery = fail_first_ack
        sent = 0

        async def sender(origin, notification) -> None:
            nonlocal sent
            sent += 1

        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await self.coordinator.deliver_one(sender, now=101)
        await self.coordinator.deliver_one(sender, now=102)

        self.assertEqual(sent, 2)

    def test_removed_group_delivery_is_cancelled(self) -> None:
        self.journal.cancel_ineligible_deliveries(["origin-ok"])
        self.assertEqual(self.journal.pending_delivery_origins(), ["origin-ok"])
```

- [ ] **Step 2: Run delivery tests and verify missing API failures**

Run:

```bash
python3 -m unittest test_comment_capture.CommentDeliveryTest
```

Expected: `ERROR` for missing delivery journal/coordinator methods.

- [ ] **Step 3: Add delivery rows, reconstruction, and acknowledgement methods**

Add to `asoul_comment_journal.py`:

```python
@dataclass(frozen=True)
class PendingCommentDelivery:
    delivery_id: int
    event_id: int
    unified_msg_origin: str
    resource: BilibiliCommentResource
    post: BilibiliCommentPost
    attempt_count: int
```

Implement `next_due_delivery(now)` by joining `event_delivery`, `comment_event`, `observed_comment`, and `resource_lifecycle`, selecting one pending row ordered by `next_attempt_at, delivery_id`. Reconstruct `image_urls` with `json.loads`, and return the exact record above.

Add:

```python
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

def cancel_ineligible_deliveries(self, active_origins: Sequence[str]) -> None:
    origins = tuple(dict.fromkeys(str(origin) for origin in active_origins))
    with self._connection:
        if not origins:
            self._connection.execute(
                "UPDATE event_delivery SET state = 'cancelled' WHERE state = 'pending'"
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
```

Add `pending_delivery_origins()` for deterministic tests and `purge_retired_lifecycles()` that deletes retired lifecycles only when no pending deliveries reference their events.

- [ ] **Step 4: Implement delivery execution and notification construction**

Add to `CommentCaptureCoordinator`:

```python
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
```

Import `Awaitable` with the other typing imports.

- [ ] **Step 5: Run delivery and full tests**

Run:

```bash
python3 -m unittest test_comment_capture.CommentDeliveryTest
python3 -m unittest
```

Expected: both commands end with `OK`.

- [ ] **Step 6: Commit per-group delivery**

```bash
git add asoul_comment_journal.py asoul_comment_capture.py test_comment_capture.py
git commit -m "feat: add at-least-once comment deliveries"
```

---

### Task 6: Integrate the Journal with AstrBot Runtime

**Files:**
- Modify: `main.py:1-63`
- Modify: `asoul_bilibili_runtime.py:1-119,639-716,769-837`
- Modify: `test_asoul_push_targets.py:1-109`
- Modify: `test_bilibili_runtime_diagnostics.py`
- Modify: `test_asoul_delivery_confirmation.py`

**Interfaces:**
- Consumes: `CommentJournal`, `CommentCaptureCoordinator`, and current `BilibiliMonitorService.discover_comment_resources`.
- Produces: `BilibiliRuntime.refresh_one_due_comment_catalog(now) -> bool`.
- Produces: `BilibiliRuntime.run_one_comment_work_item(now) -> bool`.
- Produces: `BilibiliRuntime.send_captured_comment(origin, notification) -> None`.

- [ ] **Step 1: Add failing runtime lifecycle tests**

Update the AstrBot stub in `test_asoul_push_targets.py` with a `DummyStarTools` class whose `get_data_dir()` returns a new `Path(tempfile.mkdtemp(prefix="asoul_plugin_test_"))` on every call, and expose it as `star_module.StarTools`.

Add to `test_bilibili_runtime_diagnostics.py`:

```python
def test_runtime_uses_plugin_data_directory_for_comment_database(self) -> None:
    plugin, runtime = self._new_runtime()

    self.assertEqual(runtime.comment_journal.path.name, "bilibili_comments.sqlite3")
    self.assertNotIn("plugins/astrbot_plugin_asoul", str(runtime.comment_journal.path))
    asyncio.run(plugin.terminate())

def test_comment_work_continues_without_active_groups_to_prevent_replay(self) -> None:
    _, runtime = self._new_runtime()
    runtime.push_config = replace(
        runtime.push_config,
        enabled=True,
        push_comment=True,
        target_uids=["100"],
    )
    runtime.push_targets = {}
    calls: list[str] = []

    async def record_catalog(now: int) -> bool:
        calls.append("catalog")
        return True

    runtime.refresh_one_due_comment_catalog = record_catalog
    worked = asyncio.run(runtime.run_one_comment_work_item(NOW_TS))

    self.assertTrue(worked)
    self.assertEqual(calls, ["catalog"])

def test_terminate_closes_comment_journal(self) -> None:
    plugin, runtime = self._new_runtime()
    asyncio.run(plugin.terminate())

    with self.assertRaises(Exception):
        runtime.comment_journal.pending_delivery_count()
```

Replace the old comment confirmation test in `test_asoul_delivery_confirmation.py` with one that inserts a captured event through `comment_journal.commit_scan_page`, runs `run_one_comment_work_item`, and asserts that one successful group is acknowledged while one failed group remains pending.

- [ ] **Step 2: Run runtime tests and verify constructor/API failures**

Run:

```bash
python3 -m unittest \
  test_bilibili_runtime_diagnostics.BilibiliRuntimeDiagnosticsTest.test_runtime_uses_plugin_data_directory_for_comment_database \
  test_bilibili_runtime_diagnostics.BilibiliRuntimeDiagnosticsTest.test_comment_work_continues_without_active_groups_to_prevent_replay \
  test_bilibili_runtime_diagnostics.BilibiliRuntimeDiagnosticsTest.test_terminate_closes_comment_journal
```

Expected: `ERROR` for missing `StarTools`, `comment_journal`, and new runtime methods.

- [ ] **Step 3: Resolve the production data path and inject it into the runtime**

Modify `main.py`:

```python
from astrbot.api.star import Context, Star, StarTools, register


def _build_comment_db_path() -> Path:
    data_dir = Path(StarTools.get_data_dir())
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "bilibili_comments.sqlite3"
```

Construct the runtime with:

```python
self._bilibili_runtime = BilibiliRuntime(
    self,
    context,
    self.config,
    comment_db_path=_build_comment_db_path(),
)
```

Change `BilibiliRuntime.__init__` to accept keyword-only `comment_db_path: Path`, then create:

```python
self.comment_journal = CommentJournal(comment_db_path)
self.comment_capture = CommentCaptureCoordinator(
    gateway=self.gateway,
    journal=self.comment_journal,
    classify_error=lambda exc: CommentCaptureError(**self.classify_poll_error(exc).__dict__),
    retry_policy=CommentRetryPolicy(random_value=random.random),
)
```

Import `random`, `Path`, `CommentCaptureError`, `CommentCaptureCoordinator`, `CommentRetryPolicy`, and `CommentJournal` explicitly.

- [ ] **Step 4: Implement catalog refresh and one-item runtime work**

Add:

```python
async def refresh_one_due_comment_catalog(self, now: int) -> bool:
    self.comment_journal.retire_unconfigured_owners(
        self.push_config.target_uids, now
    )
    for uid in self.push_config.target_uids:
        if not self.comment_journal.catalog_refresh_due(
            uid, now, COMMENT_RESOURCE_REFRESH_INTERVAL_SECONDS
        ):
            continue
        self.comment_journal.begin_catalog_refresh(uid, now)
        try:
            author_name = await self.gateway.get_comment_resource_owner_name(uid)
            resources = await self.monitor.discover_comment_resources(uid, author_name)
        except Exception as exc:
            error = self.classify_poll_error(exc)
            self.comment_journal.fail_catalog_refresh(
                uid, error.category, error.message
            )
            return True
        self.comment_journal.sync_resource_catalog(
            uid, author_name, resources, now
        )
        return True
    return False

async def send_captured_comment(
    self, unified_msg_origin: str, notification: Any
) -> None:
    target = next(
        (
            item
            for item in self.get_active_push_targets()
            if item.unified_msg_origin == unified_msg_origin
        ),
        None,
    )
    if target is None:
        raise RuntimeError("comment delivery target is no longer active")
    result = await self.build_notification_result(notification, target)
    await self.context.send_message(unified_msg_origin, result)

async def run_one_comment_work_item(self, now: int) -> bool:
    targets = self.get_active_push_targets()
    target_origins = [target.unified_msg_origin for target in targets]
    self.comment_journal.cancel_ineligible_deliveries(target_origins)
    if await self.comment_capture.deliver_one(self.send_captured_comment, now):
        return True
    if await self.refresh_one_due_comment_catalog(now):
        return True
    task = self.comment_journal.next_due_scan_task(now)
    if task is None:
        self.comment_journal.purge_retired_lifecycles()
        return False
    async with self.get_uid_poll_lock(task.owner_uid):
        await self.comment_capture.run_scan_task(
            task,
            target_uids=self.push_config.target_uids,
            target_origins=target_origins,
            now=now,
        )
    return True
```

Replace `_run_comment_monitor_loop` with a loop that refreshes configuration, pauses when disabled or unauthenticated, otherwise calls `run_one_comment_work_item(int(time.time()))`, and sleeps `0` after work or `1` second when idle. Preserve cancellation logging. Do not require an active group before scanning.

In `terminate`, cancel the worker first and call `self.comment_journal.close()` after both monitor tasks have stopped.

- [ ] **Step 5: Run runtime, delivery, and full tests**

Run:

```bash
python3 -m unittest test_bilibili_runtime_diagnostics test_asoul_delivery_confirmation
python3 -m unittest
```

Expected: both commands end with `OK`. Confirm the dynamic/video delivery tests still use the KV state path and the new comment tests use SQLite.

- [ ] **Step 6: Commit runtime integration**

```bash
git add \
  main.py asoul_bilibili_runtime.py test_asoul_push_targets.py \
  test_bilibili_runtime_diagnostics.py test_asoul_delivery_confirmation.py
git commit -m "feat: run durable comment capture in AstrBot"
```

---

### Task 7: Add Diagnostics and Retire the Fixed-Window Production Path

**Files:**
- Modify: `asoul_comment_journal.py`
- Modify: `asoul_bilibili_runtime.py:1067-1104`
- Modify: `asoul_bilibili.py:160-175,716-835,1566-1717,1767-1860,1983-2059,2114-2178`
- Modify: `test_bilibili_runtime_diagnostics.py`
- Modify: `test_bilibili_monitor.py`
- Modify: `_conf_schema.json`

**Interfaces:**
- Produces: `CommentJournalStatus` and `CommentJournal.status(now)`.
- Removes production use of `BilibiliCommentSnapshot`, `get_comment_window`, `get_recent_sub_comments`, `fetch_comment_snapshot`, and `plan_comment_deliveries`.
- Retains `get_recent_comments` for `/bili_test_comment` until the command is migrated separately.

- [ ] **Step 1: Write failing journal status and command-output tests**

Add this test class to `test_comment_journal.py`:

```python
class CommentJournalStatusTest(CommentJournalLifecycleTest):
    def test_status_counts_backlog_retries_and_deliveries(self) -> None:
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], now=100
        )
        task = self.journal.next_due_scan_task(100)
        assert task is not None
        self.journal.commit_scan_page(
            task=task,
            posts=[
                BilibiliCommentPost(
                    id="9002",
                    author_uid="100",
                    author_name="测试账号",
                    text="新评论",
                    created_at=101,
                    is_reply=False,
                    root_id="9002",
                )
            ],
            target_uids=["100"],
            target_origins=["origin-a", "origin-b"],
            now=101,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )
        self.journal.mark_scan_failed(
            task.task_id,
            category="risk_control",
            message="请求被拒绝",
            next_attempt_at=160,
        )

        status = self.journal.status(now=1_000)

        self.assertEqual(status.lifecycle_counts["bootstrapping"], 1)
        self.assertEqual(status.retrying_scan_count, 1)
        self.assertEqual(status.pending_delivery_count, 2)
        self.assertEqual(status.oldest_scan_due_at, 101)
```

Add to `test_bilibili_runtime_diagnostics.py`:

```python
def test_status_text_reports_journal_backlog_and_incomplete_resources(self) -> None:
    _, runtime = self._new_runtime()
    runtime.comment_journal.status = lambda now: CommentJournalStatus(
        lifecycle_counts={"bootstrapping": 1, "active": 5, "retired": 2},
        incomplete_count=1,
        pending_scan_count=4,
        overdue_scan_count=2,
        retrying_scan_count=1,
        oldest_scan_due_at=NOW_TS - 600,
        pending_delivery_count=3,
        oldest_delivery_due_at=NOW_TS - 60,
        last_reconciliation_at=NOW_TS - 1200,
    )

    text = asyncio.run(runtime.build_bilibili_status_text())

    self.assertIn("活跃资源：5", text)
    self.assertIn("不完整资源：1", text)
    self.assertIn("待抓取任务：4（逾期 2，重试 1）", text)
    self.assertIn("待投递：3", text)
```

- [ ] **Step 2: Run status tests and verify missing record failure**

Run:

```bash
python3 -m unittest \
  test_comment_journal.CommentJournalStatusTest \
  test_bilibili_runtime_diagnostics.BilibiliRuntimeDiagnosticsTest.test_status_text_reports_journal_backlog_and_incomplete_resources
```

Expected: `ERROR` for missing `CommentJournalStatus` or `status`.

- [ ] **Step 3: Implement status queries and output**

Add:

```python
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
```

Implement `status(now)` with these aggregate queries:

```python
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
        """,
        (int(now),),
    ).fetchone()
    delivery_row = self._connection.execute(
        """
        SELECT COUNT(*) AS pending,
               COALESCE(MIN(next_attempt_at), 0) AS oldest_due
        FROM event_delivery WHERE state = 'pending'
        """
    ).fetchone()
    return CommentJournalStatus(
        lifecycle_counts=lifecycle_counts,
        incomplete_count=int(incomplete_row["count"]),
        pending_scan_count=int(scan_row["pending"]),
        overdue_scan_count=int(scan_row["overdue"]),
        retrying_scan_count=int(scan_row["retrying"]),
        oldest_scan_due_at=int(scan_row["oldest_due"]),
        pending_delivery_count=int(delivery_row["pending"]),
        oldest_delivery_due_at=int(delivery_row["oldest_due"]),
        last_reconciliation_at=int(scan_row["last_success"]),
    )
```

Return zero for every missing aggregate. Update `last_success_at` whenever a page stream exhausts.

Change `build_bilibili_status_text` to append the exact counts asserted above plus formatted oldest due times and the last reconciliation time. Keep login status, request client, and configured intervals.

- [ ] **Step 4: Remove the retired production pipeline**

Remove the old fixed-window methods and tests named:

- `test_comment_window_keeps_only_first_twenty_roots`
- `test_get_recent_sub_comments_stops_at_known_reply`
- `test_first_comment_snapshot_does_not_expand_sub_comment_trees`
- `test_known_root_later_in_window_fetches_sub_comments_when_count_grows`
- old `BilibiliMonitorService.plan_comment_deliveries` state-cache tests

Keep parsing, `get_recent_comments`, resource discovery, and admin test-command coverage. Remove old runtime calls to `poll_bilibili_comments_for_uid`, old per-target comment state planning, and the unused comment resource catalog helpers. Preserve legacy JSON keys when normalizing existing KV data, but do not mutate them from the new comment worker.

Update the `push_comment` hint in `_conf_schema.json` to:

```json
"hint": "开启后会完整遍历最近 3 条动态和最近 3 个视频的评论与楼中楼；使用登录态、持久化检查点和退避重试，通知可能延迟。"
```

- [ ] **Step 5: Run focused and full regression tests**

Run:

```bash
python3 -m unittest \
  test_comment_journal test_comment_capture \
  test_bilibili_monitor test_bilibili_runtime_diagnostics \
  test_asoul_delivery_confirmation
python3 -m unittest
```

Expected: both commands end with `OK`. Confirm no test still asserts the 20-root or 3-page caps.

- [ ] **Step 6: Commit diagnostics and legacy cleanup**

```bash
git add \
  asoul_comment_journal.py asoul_bilibili_runtime.py asoul_bilibili.py \
  test_comment_journal.py test_bilibili_runtime_diagnostics.py \
  test_bilibili_monitor.py _conf_schema.json
git commit -m "refactor: retire bounded comment monitor state"
```

---

### Task 8: Add the Credential-Safe Real API Harness and Documentation

**Files:**
- Create: `tools/verify_bilibili_comments.py`
- Create: `test_bilibili_integration_harness.py`
- Modify: `README.md:1-150`
- Modify: `metadata.yaml:1-8`
- Modify: `main.py:48`

**Interfaces:**
- Consumes: real `BilibiliGateway`, `CommentJournal`, and `CommentCaptureCoordinator`.
- Produces: `load_credential_file(path, repo_root)`, `redact_error(text)`, and a CLI recording report.
- The CLI is read-only against Bilibili and writes only a temporary SQLite database and a redacted local JSON report.

- [ ] **Step 1: Write failing credential-boundary and report tests**

Create `test_bilibili_integration_harness.py`:

```python
import json
import os
import tempfile
import unittest
from pathlib import Path

from tools.verify_bilibili_comments import load_credential_file, redact_error


class BilibiliIntegrationHarnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_rejects_credential_file_inside_repository(self) -> None:
        credential_path = self.root / "cookie.json"
        credential_path.write_text('{"sessdata":"secret"}', encoding="utf-8")
        credential_path.chmod(0o600)

        with self.assertRaisesRegex(ValueError, "outside the repository"):
            load_credential_file(credential_path, repo_root=self.root)

    def test_requires_private_file_permissions(self) -> None:
        repo = self.root / "repo"
        repo.mkdir()
        credential_path = self.root / "cookie.json"
        credential_path.write_text('{"sessdata":"secret"}', encoding="utf-8")
        credential_path.chmod(0o644)

        with self.assertRaisesRegex(ValueError, "0600"):
            load_credential_file(credential_path, repo_root=repo)

    def test_redaction_removes_cookie_fields_and_html_body(self) -> None:
        text = "SESSDATA=secret bili_jct=csrf <!DOCTYPE html>blocked"

        redacted = redact_error(text)

        self.assertNotIn("secret", redacted)
        self.assertNotIn("csrf", redacted)
        self.assertNotIn("DOCTYPE", redacted)
```

- [ ] **Step 2: Run harness tests and verify missing module failure**

Run:

```bash
python3 -m unittest test_bilibili_integration_harness
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'tools.verify_bilibili_comments'`.

- [ ] **Step 3: Implement secure credential loading and read-only CLI**

Create `tools/__init__.py` and `tools/verify_bilibili_comments.py`. Implement credential loading as:

```python
def load_credential_file(path: Path, repo_root: Path) -> dict[str, str]:
    resolved = Path(path).expanduser().resolve()
    root = Path(repo_root).resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError("credential file must be outside the repository")
    if resolved.stat().st_mode & 0o077:
        raise ValueError("credential file permissions must be 0600")
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    allowed = {
        "sessdata", "bili_jct", "buvid3", "buvid4",
        "dedeuserid", "ac_time_value",
    }
    credential = {
        key: str(value).strip()
        for key, value in payload.items()
        if key in allowed and str(value).strip()
    }
    if not credential.get("sessdata"):
        raise ValueError("credential file must contain sessdata")
    return credential
```

Implement `redact_error` with case-insensitive replacements for all credential field names and collapse HTML responses to `"HTML response omitted"`.

The CLI must require `--credential-file`, `--owner-uid`, `--target-uid`, `--oid`, `--type-value`, `--resource-kind`, `--resource-url`, `--duration-seconds`, and `--report`. It must:

1. Create a `TemporaryDirectory` and journal.
2. Create one `BilibiliCommentResource` from arguments and sync it at current time.
3. Print only a random marker and instructions for the user to manually post one root comment and one reply containing that marker.
4. Run page-sized scan and local recording delivery work until duration expires.
5. Never call a Bilibili write API or AstrBot context.
6. Write JSON containing the marker, resource key, captured rpids, root/reply counts, duplicate count, retry count, elapsed seconds, and pass/fail status.
7. Exit zero only when at least one root and one reply by `target_uid` containing the marker were captured and re-scanning created no duplicate event.

Use `asyncio.run(main_async(parsed_args))`; keep credentials in memory and never serialize them to the report.

- [ ] **Step 4: Update user documentation and plugin version**

Update README comment behavior to state:

- the exact 3+3 resource boundary;
- no historical replay on activation or re-entry;
- full cursor/page traversal with durable SQLite progress;
- at-least-once delivery and possible duplicates after uncertain sends;
- 2-second request spacing and up-to-12-hour retry delay;
- `/bili_status` backlog and incomplete-resource fields;
- the real harness command using a repository-external `0600` credential file;
- the prohibition on pasting cookies into chat or committing them.

Bump `metadata.yaml` from `v3.0.2` to `v3.1.0`, and change the `@register` version in `main.py` to `v3.1.0`, matching the new persistence and delivery behavior.

- [ ] **Step 5: Run security, comment, and complete regression tests**

Run:

```bash
python3 -m unittest test_bilibili_integration_harness
python3 -m unittest \
  test_comment_journal test_comment_capture \
  test_bilibili_monitor test_bilibili_runtime_diagnostics \
  test_asoul_delivery_confirmation
python3 -m unittest
git diff --check
```

Expected: every unittest command ends with `OK`; `git diff --check` exits 0 with no output.

- [ ] **Step 6: Commit the harness and documentation**

```bash
git add \
  tools/__init__.py tools/verify_bilibili_comments.py \
  test_bilibili_integration_harness.py README.md metadata.yaml main.py
git commit -m "test: add real comment capture verification harness"
```

---

## Final Verification

- [ ] Run the full suite from a fresh process:

```bash
python3 -m unittest
```

Expected: all discovered tests pass and the command ends with `OK`.

- [ ] Verify dependency and repository state:

```bash
venv/bin/python -c "from importlib.metadata import version; assert version('bilibili-api-python') == '17.4.1'"
git diff --check
git status --short
```

Expected: the version assertion exits 0, `git diff --check` emits nothing, and `git status --short` is empty after the task commits.

- [ ] Run the real API harness only after the user has created a repository-external `0600` credential JSON and explicitly provided its local path:

```bash
venv/bin/python tools/verify_bilibili_comments.py \
  --credential-file /absolute/path/outside/repository/bilibili-cookie.json \
  --owner-uid 100 \
  --target-uid 100 \
  --oid 2003 \
  --type-value 1 \
  --resource-kind video \
  --resource-url https://www.bilibili.com/video/BV_TEST \
  --duration-seconds 900 \
  --report /tmp/asoul-comment-verification.json
```

Expected: the script performs read-only requests, prints no credential values, and exits 0 only after the controlled root comment and reply are captured without duplicate events. Replace all sample resource and UID values with the user-controlled test values before execution.
