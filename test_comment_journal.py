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


if __name__ == "__main__":
    unittest.main()
