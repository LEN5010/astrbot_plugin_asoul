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


def reply_post(rpid: str, created_at: int, root_rpid: str) -> BilibiliCommentPost:
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

    async def get_reply_comment_page(self, resource, root_id: str, page_index: int):
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
        self.assertEqual(
            self.journal.observed_rpids(first.lifecycle_id), ["9002", "9003"]
        )

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


if __name__ == "__main__":
    unittest.main()
