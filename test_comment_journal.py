import sqlite3
import tempfile
import unittest
from pathlib import Path

from asoul_bilibili import (
    BilibiliCommentPost,
    BilibiliCommentResource,
    BilibiliRichTextNode,
    BilibiliRootReplyState,
)
from asoul_comment_journal import CommentJournal


def video_resource(oid: int, published_at: int = 0) -> BilibiliCommentResource:
    return BilibiliCommentResource(
        key=f"video:{oid}",
        owner_uid="100",
        owner_name="测试账号",
        resource_kind="video",
        oid=oid,
        type_value=1,
        title=f"视频 {oid}",
        url=f"https://www.bilibili.com/video/{oid}",
        published_at=int(published_at or 0),
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

    def test_reply_gap_indexes_cover_production_query(self) -> None:
        observed_plan = self.journal._connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT COUNT(*)
            FROM observed_comment
            WHERE lifecycle_id = ? AND root_rpid = ? AND is_reply = 1
            """,
            ("lifecycle", "root"),
        ).fetchall()
        scan_index_columns = [
            str(row["name"])
            for row in self.journal._connection.execute(
                "PRAGMA index_info(ix_scan_reply_gap)"
            ).fetchall()
        ]

        self.assertIn(
            "ix_observed_root_reply",
            " ".join(str(row["detail"]) for row in observed_plan),
        )
        self.assertEqual(
            scan_index_columns,
            ["task_state", "kind", "lifecycle_id", "root_rpid"],
        )

    def test_reopening_existing_database_creates_reply_gap_indexes(self) -> None:
        self.journal._connection.execute("DROP INDEX ix_observed_root_reply")
        self.journal._connection.execute("DROP INDEX ix_scan_reply_gap")
        self.journal._connection.commit()
        self.journal.close()

        self.journal = CommentJournal(self.db_path)
        observed_indexes = {
            str(row["name"])
            for row in self.journal._connection.execute(
                "PRAGMA index_list(observed_comment)"
            ).fetchall()
        }
        scan_indexes = {
            str(row["name"])
            for row in self.journal._connection.execute(
                "PRAGMA index_list(scan_task)"
            ).fetchall()
        }

        self.assertIn("ix_observed_root_reply", observed_indexes)
        self.assertIn("ix_scan_reply_gap", scan_indexes)

    def test_reopening_legacy_database_adds_comment_rich_nodes_column(self) -> None:
        self.journal.close()
        self.db_path.unlink()
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            """
            CREATE TABLE observed_comment (
                lifecycle_id TEXT NOT NULL,
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
            )
            """
        )
        connection.commit()
        connection.close()

        self.journal = CommentJournal(self.db_path)
        columns = {
            str(row["name"])
            for row in self.journal._connection.execute(
                "PRAGMA table_info(observed_comment)"
            ).fetchall()
        }

        self.assertIn("rich_nodes_json", columns)

    def test_catalog_sync_creates_independent_head_and_reconcile_lanes(self) -> None:
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], now=100
        )

        head = self.journal.next_due_scan_task(100, lane="head", owner_uid="100")
        reconcile = self.journal.next_due_scan_task(
            100, lane="reconcile", owner_uid="100"
        )

        self.assertIsNotNone(head)
        self.assertIsNotNone(reconcile)
        assert head is not None and reconcile is not None
        self.assertEqual(head.scan_lane, "head")
        self.assertEqual(reconcile.scan_lane, "reconcile")
        self.assertNotEqual(head.task_id, reconcile.task_id)

    def test_retired_resource_reappears_as_a_new_lifecycle(self) -> None:
        first = self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        ).activated[0]
        first_missing = self.journal.sync_resource_catalog(
            "100", "测试账号", [], 200
        )
        self.assertEqual(first_missing.retired, ())
        retired = self.journal.sync_resource_catalog(
            "100", "测试账号", [], 300
        ).retired[0]
        second = self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 400
        ).activated[0]

        self.assertEqual(retired.lifecycle_id, first.lifecycle_id)
        self.assertNotEqual(second.lifecycle_id, first.lifecycle_id)
        self.assertEqual(second.entered_at, 400)

    def test_catalog_presence_resets_single_missing_snapshot(self) -> None:
        first = self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        ).activated[0]

        self.assertEqual(
            self.journal.sync_resource_catalog("100", "测试账号", [], 200).retired,
            (),
        )
        present = self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 300
        )
        self.assertEqual(present.activated, ())
        self.assertEqual(present.retired, ())
        self.assertEqual(
            self.journal.sync_resource_catalog("100", "测试账号", [], 400).retired,
            (),
        )
        active = self.journal._connection.execute(
            "SELECT lifecycle_id FROM resource_lifecycle WHERE retired_at = 0"
        ).fetchone()
        self.assertEqual(active["lifecycle_id"], first.lifecycle_id)

    def test_retirement_preserves_scan_and_observation_rows(self) -> None:
        lifecycle = self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        ).activated[0]
        head = self.journal.next_due_scan_task(100, lane="head", owner_uid="100")
        assert head is not None
        self.journal.commit_scan_page(
            task=head,
            posts=[
                BilibiliCommentPost(
                    id="9001",
                    author_uid="200",
                    author_name="观众",
                    text="历史评论",
                    created_at=99,
                    is_reply=False,
                    root_id="9001",
                )
            ],
            root_states=[BilibiliRootReplyState("9001", 0, ())],
            target_uids=["100"],
            target_origins=[],
            now=101,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )
        before_tasks = self.journal._connection.execute(
            "SELECT COUNT(*) FROM scan_task WHERE lifecycle_id = ?",
            (lifecycle.lifecycle_id,),
        ).fetchone()[0]

        self.journal.sync_resource_catalog("100", "测试账号", [], 200)
        self.journal.sync_resource_catalog("100", "测试账号", [], 300)

        after_tasks = self.journal._connection.execute(
            "SELECT COUNT(*) FROM scan_task WHERE lifecycle_id = ?",
            (lifecycle.lifecycle_id,),
        ).fetchone()[0]
        observed = self.journal.observed_rpids(lifecycle.lifecycle_id)
        self.assertEqual(after_tasks, before_tasks)
        self.assertEqual(observed, ["9001"])
        self.assertIsNone(
            self.journal.next_due_scan_task(1_000, owner_uid="100")
        )

    def test_catalog_attempt_survives_restart(self) -> None:
        self.journal.begin_catalog_refresh("100", now=100)
        self.journal.fail_catalog_refresh(
            "100",
            category="network",
            message="timeout",
            next_attempt_at=160,
        )
        self.journal.close()
        self.journal = CommentJournal(self.db_path)

        self.assertFalse(
            self.journal.catalog_refresh_due("100", now=159, interval_seconds=600)
        )
        self.assertTrue(
            self.journal.catalog_refresh_due("100", now=160, interval_seconds=600)
        )
        self.assertEqual(self.journal.catalog_retry_count("100"), 1)

    def test_legacy_task_rows_migrate_without_deletion_or_cursor_loss(self) -> None:
        self.journal.close()
        self.db_path.unlink()
        connection = sqlite3.connect(self.db_path)
        connection.executescript(
            """
            CREATE TABLE resource_lifecycle (
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
                state TEXT NOT NULL,
                incomplete_reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE scan_task (
                task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                lifecycle_id TEXT NOT NULL REFERENCES resource_lifecycle(lifecycle_id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
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
            INSERT INTO resource_lifecycle VALUES(
                'life-1', '100', '测试账号', 'video:2003', 'video',
                2003, 1, '视频', 'https://example.test/video',
                100, 0, 'bootstrapping', ''
            );
            INSERT INTO scan_task(
                lifecycle_id, kind, root_rpid, cursor, page_index,
                bootstrap_pending, next_attempt_at
            ) VALUES('life-1', 'primary', '', 'page-7', 1, 1, 100);
            WITH RECURSIVE reply_rows(value) AS (
                SELECT 1
                UNION ALL
                SELECT value + 1 FROM reply_rows WHERE value < 20000
            )
            INSERT INTO scan_task(
                lifecycle_id, kind, root_rpid, cursor, page_index,
                bootstrap_pending, next_attempt_at
            )
            SELECT 'life-1', 'reply', CAST(900000 + value AS TEXT),
                   '', 1, 1, 100
            FROM reply_rows;
            """
        )
        connection.commit()
        connection.close()

        self.journal = CommentJournal(self.db_path)

        task_count = self.journal._connection.execute(
            "SELECT COUNT(*) FROM scan_task"
        ).fetchone()[0]
        primary = self.journal._connection.execute(
            """
            SELECT cursor, scan_lane FROM scan_task
            WHERE kind = 'primary' AND root_rpid = ''
            """
        ).fetchone()
        dormant_count = self.journal._connection.execute(
            "SELECT COUNT(*) FROM scan_task WHERE task_state = 'dormant'"
        ).fetchone()[0]
        head_count = self.journal._connection.execute(
            "SELECT COUNT(*) FROM scan_task WHERE scan_lane = 'head'"
        ).fetchone()[0]
        self.assertEqual(task_count, 20_002)
        self.assertEqual(primary["cursor"], "page-7")
        self.assertEqual(primary["scan_lane"], "reconcile")
        self.assertEqual(dormant_count, 20_000)
        self.assertEqual(head_count, 1)
        self.journal.close()
        self.journal = CommentJournal(self.db_path)
        reopened_count = self.journal._connection.execute(
            "SELECT COUNT(*) FROM scan_task"
        ).fetchone()[0]
        self.assertEqual(reopened_count, 20_002)


class CommentJournalPageCommitTest(CommentJournalLifecycleTest):
    def _activate_resource(self, entered_at: int = 100):
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], entered_at
        )
        task = self.journal.next_due_scan_task(entered_at)
        assert task is not None
        return task

    def test_page_commit_suppresses_history_and_enqueues_same_second(self) -> None:
        # published_at defaults to 0 → fallback baseline uses entered_at - grace.
        # With entered_at=100 and grace=15min, cutoff is 0, so both posts are new.
        # Use explicit published_at to model pre-publish history.
        self.journal.close()
        self.journal = CommentJournal(self.db_path)
        self.journal.sync_resource_catalog(
            "100",
            "测试账号",
            [video_resource(2003, published_at=100)],
            now=105,
        )
        task = self.journal.next_due_scan_task(105)
        assert task is not None
        posts = [
            BilibiliCommentPost(
                id="9001",
                author_uid="100",
                author_name="测试账号",
                text="历史评论",
                created_at=99,
                is_reply=False,
                root_id="9001",
            ),
            BilibiliCommentPost(
                id="9002",
                author_uid="100",
                author_name="测试账号",
                text="边界评论",
                created_at=100,
                is_reply=False,
                root_id="9002",
            ),
        ]

        result = self.journal.commit_scan_page(
            task=task,
            posts=posts,
            target_uids=["100"],
            target_origins=["aiocqhttp:GroupMessage:1"],
            now=106,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=286,
        )

        self.assertEqual(result.events_created, 1)
        self.assertEqual(result.deliveries_created, 1)
        self.assertEqual(
            self.journal.observed_rpids(task.lifecycle_id), ["9001", "9002"]
        )

    def test_baseline_uses_resource_published_at_not_entered_at(self) -> None:
        self.journal.sync_resource_catalog(
            "100",
            "测试账号",
            [video_resource(2003, published_at=90)],
            now=100,
        )
        task = self.journal.next_due_scan_task(100)
        assert task is not None
        # Comment between publish and catalog entry must still create an event.
        result = self.journal.commit_scan_page(
            task=task,
            posts=[
                BilibiliCommentPost(
                    id="9002",
                    author_uid="100",
                    author_name="测试账号",
                    text="发稿后进监控前的自评",
                    created_at=95,
                    is_reply=False,
                    root_id="9002",
                )
            ],
            target_uids=["100"],
            target_origins=["origin-a"],
            now=101,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )
        self.assertEqual(result.events_created, 1)
        self.assertEqual(result.deliveries_created, 1)

    def test_reentered_resource_does_not_replay_observed_comment(self) -> None:
        resource = video_resource(2003, published_at=90)
        first_lifecycle = self.journal.sync_resource_catalog(
            "100", "测试账号", [resource], now=100
        ).activated[0]
        first_task = self.journal.next_due_scan_task(100)
        assert first_task is not None
        post = BilibiliCommentPost(
            id="pinned-9002",
            author_uid="100",
            author_name="测试账号",
            text="置顶评论",
            created_at=95,
            is_reply=False,
            root_id="pinned-9002",
        )
        first = self.journal.commit_scan_page(
            task=first_task,
            posts=[post],
            target_uids=["100"],
            target_origins=["origin-a"],
            now=101,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )
        self.assertEqual(first.events_created, 1)

        self.journal.sync_resource_catalog("100", "测试账号", [], now=200)
        retired = self.journal.sync_resource_catalog(
            "100", "测试账号", [], now=300
        ).retired[0]
        self.assertEqual(retired.lifecycle_id, first_lifecycle.lifecycle_id)
        second_lifecycle = self.journal.sync_resource_catalog(
            "100", "测试账号", [resource], now=400
        ).activated[0]
        second_task = self.journal.next_due_scan_task(
            400, owner_uid="100"
        )
        assert second_task is not None
        second = self.journal.commit_scan_page(
            task=second_task,
            posts=[post],
            target_uids=["100"],
            target_origins=["origin-a"],
            now=401,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=581,
        )

        self.assertNotEqual(
            second_lifecycle.lifecycle_id, first_lifecycle.lifecycle_id
        )
        self.assertEqual(second.events_created, 0)
        self.assertEqual(second.deliveries_created, 0)
        baseline = self.journal._connection.execute(
            """
            SELECT baseline FROM observed_comment
            WHERE lifecycle_id = ? AND rpid = ?
            """,
            (second_lifecycle.lifecycle_id, post.id),
        ).fetchone()
        self.assertEqual(int(baseline["baseline"]), 1)

    def test_comments_older_than_one_day_are_observed_without_notification(
        self,
    ) -> None:
        self.journal.sync_resource_catalog(
            "100",
            "测试账号",
            [video_resource(2003, published_at=1)],
            now=100_000,
        )
        task = self.journal.next_due_scan_task(100_000)
        assert task is not None
        result = self.journal.commit_scan_page(
            task=task,
            posts=[
                BilibiliCommentPost(
                    id="old",
                    author_uid="100",
                    author_name="测试账号",
                    text="超过一天",
                    created_at=13_599,
                    is_reply=False,
                    root_id="old",
                ),
                BilibiliCommentPost(
                    id="boundary",
                    author_uid="100",
                    author_name="测试账号",
                    text="刚好一天",
                    created_at=13_600,
                    is_reply=False,
                    root_id="boundary",
                ),
            ],
            target_uids=["100"],
            target_origins=["origin-a"],
            now=100_000,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=100_180,
        )

        self.assertEqual(result.events_created, 1)
        self.assertEqual(result.deliveries_created, 1)
        self.assertEqual(
            self.journal.observed_rpids(task.lifecycle_id),
            ["boundary", "old"],
        )

    def test_comment_rich_nodes_survive_delivery_persistence(self) -> None:
        task = self._activate_resource()
        self.journal.commit_scan_page(
            task=task,
            posts=[
                BilibiliCommentPost(
                    id="9002",
                    author_uid="100",
                    author_name="测试账号",
                    text="看看[嘉然_暗中观察]",
                    created_at=101,
                    is_reply=False,
                    root_id="9002",
                    rich_nodes=[
                        BilibiliRichTextNode(kind="text", text="看看"),
                        BilibiliRichTextNode(
                            kind="emoji",
                            text="[嘉然_暗中观察]",
                            image_url="https://i0.hdslb.com/emote.png",
                        ),
                    ],
                )
            ],
            target_uids=["100"],
            target_origins=["origin-a"],
            now=102,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=282,
        )

        delivery = self.journal.next_due_delivery(102)

        assert delivery is not None
        self.assertEqual(
            [
                (node.kind, node.text, node.image_url)
                for node in delivery.post.rich_nodes
            ],
            [
                ("text", "看看", ""),
                (
                    "emoji",
                    "[嘉然_暗中观察]",
                    "https://i0.hdslb.com/emote.png",
                ),
            ],
        )

    def test_duplicate_page_is_idempotent(self) -> None:
        task = self._activate_resource()
        post = BilibiliCommentPost(
            id="9002",
            author_uid="100",
            author_name="测试账号",
            text="新评论",
            created_at=101,
            is_reply=False,
            root_id="9002",
        )
        arguments = dict(
            task=task,
            posts=[post],
            target_uids=["100"],
            target_origins=["origin-a", "origin-b"],
            now=102,
            next_cursor="page-2",
            next_page_index=0,
            next_sweep_at=102,
        )
        first = self.journal.commit_scan_page(**arguments)
        second = self.journal.commit_scan_page(**arguments)

        self.assertEqual(first.events_created, 1)
        self.assertEqual(second.events_created, 0)
        self.assertEqual(self.journal.pending_delivery_count(), 2)

    def test_unchanged_root_reply_state_keeps_reply_task_dormant(self) -> None:
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        )
        head = self.journal.next_due_scan_task(100, lane="head", owner_uid="100")
        assert head is not None
        root = BilibiliCommentPost(
            id="9001",
            author_uid="200",
            author_name="观众",
            text="一级评论",
            created_at=101,
            is_reply=False,
            root_id="9001",
            reply_count=0,
        )
        self.journal.commit_scan_page(
            task=head,
            posts=[root],
            root_states=[BilibiliRootReplyState("9001", 0, ())],
            target_uids=["100"],
            target_origins=[],
            now=101,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )

        self.assertIsNone(
            self.journal.next_due_scan_task(1_000, lane="reply", owner_uid="100")
        )

    def test_reply_count_change_activates_dormant_reply_task(self) -> None:
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        )
        head = self.journal.next_due_scan_task(100, lane="head", owner_uid="100")
        assert head is not None
        root = BilibiliCommentPost(
            id="9001",
            author_uid="200",
            author_name="观众",
            text="一级评论",
            created_at=101,
            is_reply=False,
            root_id="9001",
            reply_count=0,
        )
        self.journal.commit_scan_page(
            task=head,
            posts=[root],
            root_states=[BilibiliRootReplyState("9001", 0, ())],
            target_uids=["100"],
            target_origins=[],
            now=101,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )
        head = self.journal.next_due_scan_task(281, lane="head", owner_uid="100")
        assert head is not None
        self.journal.commit_scan_page(
            task=head,
            posts=[root],
            root_states=[BilibiliRootReplyState("9001", 1, ("9002",))],
            target_uids=["100"],
            target_origins=[],
            now=281,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=461,
        )

        reply = self.journal.next_due_scan_task(
            281, lane="reply", owner_uid="100"
        )
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertEqual(reply.root_rpid, "9001")
        self.assertEqual(reply.task_state, "scheduled")

    def test_embedded_reply_fingerprint_change_reactivates_reply_task(self) -> None:
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        )
        head = self.journal.next_due_scan_task(100, lane="head", owner_uid="100")
        assert head is not None
        root = BilibiliCommentPost(
            id="9001",
            author_uid="200",
            author_name="观众",
            text="一级评论",
            created_at=101,
            is_reply=False,
            root_id="9001",
            reply_count=1,
        )
        self.journal.commit_scan_page(
            head,
            [root],
            ["100"],
            [],
            101,
            root_states=[BilibiliRootReplyState("9001", 1, ("9002",))],
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )
        reply = self.journal.next_due_scan_task(
            101, lane="reply", owner_uid="100"
        )
        assert reply is not None
        self.journal.commit_scan_page(
            reply,
            [],
            ["100"],
            [],
            102,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=0,
        )
        head = self.journal.next_due_scan_task(281, lane="head", owner_uid="100")
        assert head is not None
        self.journal.commit_scan_page(
            head,
            [root],
            ["100"],
            [],
            281,
            root_states=[BilibiliRootReplyState("9001", 1, ("9003",))],
            next_cursor="",
            next_page_index=0,
            next_sweep_at=461,
        )

        reactivated = self.journal.next_due_scan_task(
            281, lane="reply", owner_uid="100"
        )
        self.assertIsNotNone(reactivated)

    def test_completed_reply_does_not_schedule_periodic_safety(self) -> None:
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        )
        head = self.journal.next_due_scan_task(100, lane="head", owner_uid="100")
        assert head is not None
        root = BilibiliCommentPost(
            id="9001",
            author_uid="200",
            author_name="观众",
            text="一级评论",
            created_at=101,
            is_reply=False,
            root_id="9001",
            reply_count=1,
        )
        self.journal.commit_scan_page(
            head,
            [root],
            ["100"],
            [],
            101,
            root_states=[BilibiliRootReplyState("9001", 1, ())],
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )
        reply = self.journal.next_due_scan_task(
            101, lane="reply", owner_uid="100"
        )
        assert reply is not None
        self.journal.commit_scan_page(
            reply,
            [],
            ["100"],
            [],
            102,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=0,
        )
        row = self.journal._connection.execute(
            """
            SELECT next_safety_scan_at, known_reply_count, reconciled_reply_count
            FROM comment_root_state
            WHERE lifecycle_id = ? AND root_rpid = '9001'
            """,
            (head.lifecycle_id,),
        ).fetchone()
        self.assertEqual(int(row["next_safety_scan_at"]), 0)
        self.assertEqual(self.journal.activate_reply_gaps(102 + 24 * 60 * 60), 0)
        self.assertIsNone(
            self.journal.next_due_scan_task(
                102 + 24 * 60 * 60, lane="reply", owner_uid="100"
            )
        )

    def test_reply_gap_vs_observed_reactivates_dormant_task(self) -> None:
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        )
        head = self.journal.next_due_scan_task(100, lane="head", owner_uid="100")
        assert head is not None
        root = BilibiliCommentPost(
            id="9001",
            author_uid="200",
            author_name="观众",
            text="一级评论",
            created_at=101,
            is_reply=False,
            root_id="9001",
            reply_count=1,
        )
        self.journal.commit_scan_page(
            head,
            [root],
            ["100"],
            [],
            101,
            root_states=[BilibiliRootReplyState("9001", 1, ())],
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )
        reply = self.journal.next_due_scan_task(
            101, lane="reply", owner_uid="100"
        )
        assert reply is not None
        self.journal.commit_scan_page(
            reply,
            [],
            ["100"],
            [],
            102,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=0,
        )
        # Simulate API reporting more replies than we have archived.
        self.journal._connection.execute(
            """
            UPDATE comment_root_state
            SET known_reply_count = 3, reconciled_reply_count = 0
            WHERE lifecycle_id = ? AND root_rpid = '9001'
            """,
            (head.lifecycle_id,),
        )
        self.journal._connection.commit()
        self.assertEqual(self.journal.activate_reply_gaps(200), 1)
        reactivated = self.journal.next_due_scan_task(
            200, lane="reply", owner_uid="100"
        )
        self.assertIsNotNone(reactivated)
        assert reactivated is not None
        self.assertEqual(reactivated.root_rpid, "9001")

    def test_deleted_comment_error_retires_reply_task(self) -> None:
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        )
        head = self.journal.next_due_scan_task(100, lane="head", owner_uid="100")
        assert head is not None
        self.journal.commit_scan_page(
            head,
            [
                BilibiliCommentPost(
                    id="9001",
                    author_uid="200",
                    author_name="观众",
                    text="一级评论",
                    created_at=101,
                    is_reply=False,
                    root_id="9001",
                    reply_count=1,
                )
            ],
            ["100"],
            [],
            101,
            root_states=[BilibiliRootReplyState("9001", 1, ())],
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )
        reply = self.journal.next_due_scan_task(
            101, lane="reply", owner_uid="100"
        )
        assert reply is not None
        self.journal.mark_scan_failed(
            reply.task_id,
            category="api",
            message="已经被删除了",
            next_attempt_at=200,
            attempted_at=102,
        )
        row = self.journal._connection.execute(
            "SELECT task_state, last_error_category FROM scan_task WHERE task_id = ?",
            (reply.task_id,),
        ).fetchone()
        self.assertEqual(row["task_state"], "retired")
        self.assertEqual(row["last_error_category"], "gone")
        self.assertIsNone(
            self.journal.next_due_scan_task(10_000, lane="reply", owner_uid="100")
        )

    def test_reply_state_change_precedes_unrelated_gap(self) -> None:
        self.journal.sync_resource_catalog(
            "100", "测试账号", [video_resource(2003)], 100
        )
        head = self.journal.next_due_scan_task(100, lane="head", owner_uid="100")
        assert head is not None
        old_root = BilibiliCommentPost(
            id="9001",
            author_uid="200",
            author_name="观众",
            text="已有回复的旧楼层",
            created_at=101,
            is_reply=False,
            root_id="9001",
            reply_count=1,
        )
        self.journal.commit_scan_page(
            head,
            [old_root],
            ["100"],
            [],
            101,
            root_states=[BilibiliRootReplyState("9001", 1, ())],
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )
        old_reply = self.journal.next_due_scan_task(
            101, lane="reply", owner_uid="100"
        )
        assert old_reply is not None
        self.journal.commit_scan_page(
            old_reply,
            [],
            ["100"],
            [],
            102,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=0,
        )

        head = self.journal.next_due_scan_task(281, lane="head", owner_uid="100")
        assert head is not None
        new_root = BilibiliCommentPost(
            id="9002",
            author_uid="201",
            author_name="新观众",
            text="刚出现回复的新楼层",
            created_at=103,
            is_reply=False,
            root_id="9002",
            reply_count=1,
        )
        self.journal.commit_scan_page(
            head,
            [new_root, old_root],
            ["100"],
            [],
            281,
            root_states=[
                BilibiliRootReplyState("9002", 1, ()),
                BilibiliRootReplyState("9001", 1, ()),
            ],
            next_cursor="",
            next_page_index=0,
            next_sweep_at=461,
        )

        due_roots = []
        for _ in range(3):
            selected = self.journal.next_due_scan_task(
                281, lane="reply", owner_uid="100"
            )
            if selected is None:
                break
            due_roots.append(selected.root_rpid)
            # Advance the selected task so the next due root can surface.
            self.journal._connection.execute(
                """
                UPDATE scan_task
                SET task_state = 'dormant', next_attempt_at = 0,
                    reply_change_pending = 0
                WHERE task_id = ?
                """,
                (selected.task_id,),
            )
            self.journal._connection.commit()
        self.assertIn("9002", due_roots)
        self.assertGreaterEqual(
            self.journal.status(281).reply_change_pending_count,
            0,
        )

    def test_invalid_ctime_rolls_back_observation_and_cursor(self) -> None:
        task = self._activate_resource()
        post = BilibiliCommentPost(
            id="9002",
            author_uid="100",
            author_name="测试账号",
            text="无时间",
            created_at=0,
            is_reply=False,
            root_id="9002",
        )

        with self.assertRaisesRegex(ValueError, "valid ctime"):
            self.journal.commit_scan_page(
                task,
                [post],
                ["100"],
                ["origin-a"],
                102,
                next_cursor="page-2",
                next_page_index=0,
                next_sweep_at=102,
            )

        reloaded = self.journal.next_due_scan_task(102)
        assert reloaded is not None
        self.assertEqual(reloaded.cursor, "")
        self.assertEqual(self.journal.observed_rpids(task.lifecycle_id), [])


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
            attempted_at=102,
        )

        status = self.journal.status(now=1_000)

        self.assertEqual(status.lifecycle_counts["bootstrapping"], 1)
        self.assertEqual(status.retrying_scan_count, 1)
        self.assertEqual(status.pending_delivery_count, 2)
        self.assertEqual(status.oldest_scan_due_at, 100)
        self.assertEqual(status.lane_due_counts["head"], 1)
        self.assertEqual(status.lane_due_counts["reconcile"], 1)
        self.assertEqual(status.dormant_reply_count, 1)
        self.assertEqual(status.request_count_15m, 2)
        self.assertEqual(status.request_count_60m, 2)
        minute_rows = self.journal._connection.execute(
            "SELECT COUNT(*) FROM comment_scan_minute"
        ).fetchone()[0]
        self.assertEqual(minute_rows, 1)


if __name__ == "__main__":
    unittest.main()
