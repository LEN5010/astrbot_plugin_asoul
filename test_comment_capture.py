import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from asoul_bilibili import (
    BilibiliCommentPost,
    BilibiliReplyCommentPage,
    BilibiliRootCommentPage,
    BilibiliRootReplyState,
)
from asoul_comment_capture import (
    CommentCaptureCoordinator,
    CommentCaptureError,
    CommentRetryPolicy,
    CommentWorkScheduler,
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


class FakeSchedulingJournal:
    def __init__(self, available_lanes=("head", "reply", "reconcile")) -> None:
        self.available_lanes = set(available_lanes)
        self.calls: list[tuple[str, str]] = []

    def next_due_scan_task(self, now, *, lane=None, owner_uid=None):
        normalized_lane = str(lane or "")
        normalized_uid = str(owner_uid or "")
        self.calls.append((normalized_lane, normalized_uid))
        if normalized_lane not in self.available_lanes:
            return None
        return SimpleNamespace(scan_lane=normalized_lane, owner_uid=normalized_uid)


class CommentWorkSchedulerTest(unittest.TestCase):
    def test_weighted_cycle_is_five_three_two(self) -> None:
        journal = FakeSchedulingJournal()
        scheduler = CommentWorkScheduler()

        tasks = [scheduler.next_task(journal, 100, ["100", "200"]) for _ in range(10)]
        lanes = [task.scan_lane for task in tasks if task is not None]

        self.assertEqual(lanes.count("head"), 5)
        self.assertEqual(lanes.count("reply"), 3)
        self.assertEqual(lanes.count("reconcile"), 2)

    def test_empty_lane_lends_slot_and_owner_rotation_is_fair(self) -> None:
        journal = FakeSchedulingJournal(("head",))
        scheduler = CommentWorkScheduler()

        tasks = [scheduler.next_task(journal, 100, ["100", "200"]) for _ in range(6)]

        self.assertTrue(all(task is not None for task in tasks))
        self.assertEqual(
            [task.owner_uid for task in tasks if task is not None],
            ["100", "200", "100", "200", "100", "200"],
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

    async def test_first_head_page_activates_before_history_reconcile(self) -> None:
        self.gateway.root_pages = {
            "": BilibiliRootCommentPage(
                posts=[root_post("9001", 101)],
                next_offset="page-2",
                root_states=[BilibiliRootReplyState("9001", 1, ())],
            )
        }
        head = self.journal.next_due_scan_task(
            100, lane="head", owner_uid="100"
        )
        assert head is not None

        await self.coordinator.run_scan_task(head, ["100"], ["origin-a"], 101)

        lifecycle = self.journal._connection.execute(
            """
            SELECT state, head_ready_at, baseline_completed_at
            FROM resource_lifecycle WHERE lifecycle_id = ?
            """,
            (head.lifecycle_id,),
        ).fetchone()
        self.assertEqual(lifecycle["state"], "active")
        self.assertEqual(lifecycle["head_ready_at"], 101)
        self.assertEqual(lifecycle["baseline_completed_at"], 0)
        self.assertIsNotNone(
            self.journal.next_due_scan_task(
                101, lane="reconcile", owner_uid="100"
            )
        )
        self.assertIsNone(
            self.journal.next_due_scan_task(101, lane="head", owner_uid="100")
        )

    async def test_head_burst_follows_pages_until_known_checkpoint(self) -> None:
        self.gateway.root_pages = {
            "": BilibiliRootCommentPage(
                posts=[comment_post("9001", 101)],
                root_states=[BilibiliRootReplyState("9001", 0, ())],
            )
        }
        head = self.journal.next_due_scan_task(
            100, lane="head", owner_uid="100"
        )
        assert head is not None
        await self.coordinator.run_scan_task(head, ["100"], ["origin-a"], 101)
        self.gateway.root_pages = {
            "": BilibiliRootCommentPage(
                posts=[comment_post("9003", 281)],
                next_offset="page-2",
                root_states=[BilibiliRootReplyState("9003", 0, ())],
            ),
            "page-2": BilibiliRootCommentPage(
                posts=[comment_post("9001", 101)],
                next_offset="page-3",
                root_states=[BilibiliRootReplyState("9001", 0, ())],
            ),
        }

        first = self.journal.next_due_scan_task(
            281, lane="head", owner_uid="100"
        )
        assert first is not None
        await self.coordinator.run_scan_task(first, ["100"], ["origin-a"], 281)
        second = self.journal.next_due_scan_task(
            281, lane="head", owner_uid="100"
        )
        assert second is not None
        self.assertEqual(second.cursor, "page-2")
        await self.coordinator.run_scan_task(second, ["100"], ["origin-a"], 282)

        self.assertIsNone(
            self.journal.next_due_scan_task(282, lane="head", owner_uid="100")
        )
        self.assertIn("9003", self.journal.observed_rpids(head.lifecycle_id))

    async def test_reply_scan_continues_past_three_pages(self) -> None:
        self.gateway.root_pages = {
            "": BilibiliRootCommentPage(
                posts=[root_post("9001", 101)], next_offset=""
            )
        }
        primary = self.journal.next_due_scan_task(
            100, lane="reconcile", owner_uid="100"
        )
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
            task = self.journal.next_due_scan_task(
                now, lane="reply", owner_uid="100"
            )
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

    async def test_page_timeout_preserves_cursor_and_schedules_retry(self) -> None:
        async def wait_forever(resource, offset=""):
            await asyncio.Event().wait()

        self.gateway.get_root_comment_page = wait_forever
        self.coordinator = CommentCaptureCoordinator(
            gateway=self.gateway,
            journal=self.journal,
            classify_error=lambda exc: CommentCaptureError(
                category="timeout", code="20", message="请求超时"
            ),
            retry_policy=CommentRetryPolicy(random_value=lambda: 0.5),
            request_timeout_seconds=0.01,
        )
        task = self.journal.next_due_scan_task(
            100, lane="reconcile", owner_uid="100"
        )
        assert task is not None

        await self.coordinator.run_scan_task(task, ["100"], ["origin-a"], 100)

        retried = self.journal.next_due_scan_task(
            160, lane="reconcile", owner_uid="100"
        )
        self.assertIsNotNone(retried)
        assert retried is not None
        self.assertEqual(retried.cursor, "")
        self.assertEqual(retried.retry_count, 1)

    async def test_completed_root_is_revisited_for_late_reply(self) -> None:
        self.gateway.root_pages = {
            "": BilibiliRootCommentPage(
                posts=[root_post("9001", 101)], next_offset=""
            )
        }
        primary = self.journal.next_due_scan_task(
            100, lane="reconcile", owner_uid="100"
        )
        assert primary is not None
        await self.coordinator.run_scan_task(primary, ["100"], ["origin-a"], 101)
        first_reply_scan = self.journal.next_due_scan_task(
            101, lane="reply", owner_uid="100"
        )
        assert first_reply_scan is not None
        await self.coordinator.run_scan_task(
            first_reply_scan, ["100"], ["origin-a"], 102
        )

        self.gateway.root_pages[""] = BilibiliRootCommentPage(
            posts=[
                BilibiliCommentPost(
                    **{
                        **root_post("9001", 101).__dict__,
                        "reply_count": 2,
                    }
                )
            ],
            next_offset="",
        )
        head_rescan = self.journal.next_due_scan_task(
            1_902, lane="head", owner_uid="100"
        )
        assert head_rescan is not None
        await self.coordinator.run_scan_task(
            head_rescan, ["100"], ["origin-a"], 1_902
        )
        self.gateway.reply_pages[("9001", 1)] = BilibiliReplyCommentPage(
            posts=[reply_post("9002", 1_901, "9001")],
            next_page_index=0,
        )
        late_reply_scan = self.journal.next_due_scan_task(
            1_902, lane="reply", owner_uid="100"
        )
        assert late_reply_scan is not None
        await self.coordinator.run_scan_task(
            late_reply_scan, ["100"], ["origin-a"], 1_902
        )

        self.assertIn("9002", self.journal.observed_rpids(primary.lifecycle_id))

    def test_retry_delay_is_capped_at_twelve_hours(self) -> None:
        policy = CommentRetryPolicy(random_value=lambda: 0.5)

        self.assertEqual(policy.delay_seconds(20), 43_200)


class CommentDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await CommentCaptureCoordinatorTest.asyncSetUp(self)
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

    async def asyncTearDown(self) -> None:
        await CommentCaptureCoordinatorTest.asyncTearDown(self)

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

    async def test_delivery_notification_has_stable_card_identity(self) -> None:
        notifications = []

        async def sender(origin, notification) -> None:
            notifications.append(notification)

        await self.coordinator.deliver_one(sender, now=101)

        self.assertEqual(notifications[0].content_id, "9002")
        self.assertEqual(notifications[0].published_at, 101)

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


if __name__ == "__main__":
    unittest.main()
