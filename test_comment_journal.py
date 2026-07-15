import tempfile
import unittest
from pathlib import Path

from asoul_bilibili import BilibiliCommentPost, BilibiliCommentResource
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
            now=101,
            next_cursor="",
            next_page_index=0,
            next_sweep_at=281,
        )

        self.assertEqual(result.events_created, 1)
        self.assertEqual(result.deliveries_created, 1)
        self.assertEqual(
            self.journal.observed_rpids(task.lifecycle_id), ["9001", "9002"]
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


if __name__ == "__main__":
    unittest.main()
