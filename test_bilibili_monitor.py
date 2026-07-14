import asyncio
import copy
import json
import types
import unittest
from dataclasses import replace
from dataclasses import fields as dataclass_fields
from unittest.mock import patch

from asoul_bilibili import (
    BilibiliAdditionalCard,
    BilibiliAuthorCardProfile,
    BilibiliCommentPost,
    BilibiliCommentResource,
    BilibiliCommentSnapshot,
    BilibiliDynamicPost,
    BilibiliEngagementStats,
    BilibiliForwardedContent,
    BilibiliGateway,
    BilibiliLiveStatus,
    BilibiliMonitorService,
    BilibiliNotification,
    BilibiliPushConfig,
    BilibiliRichTextNode,
    BilibiliUidSnapshot,
    BilibiliVideoPost,
    build_bilibili_push_config,
    normalize_bilibili_uid,
)

NOW_TS = 1_700_000_000


class BilibiliPublicTypeCompatibilityTest(unittest.TestCase):
    def test_new_card_fields_are_appended_to_preserve_positional_order(self) -> None:
        dynamic_fields = [item.name for item in dataclass_fields(BilibiliDynamicPost)]
        notification_fields = [
            item.name for item in dataclass_fields(BilibiliNotification)
        ]

        self.assertEqual(dynamic_fields[-1:], ["video_bvid"])
        self.assertEqual(
            notification_fields[-2:],
            ["video_bvid", "stats_are_fallback"],
        )


class FakeBilibiliGateway:
    def __init__(self) -> None:
        self.names = {"100": "测试账号"}
        self.comment_fetch_requests = []
        self.dynamic_posts = {
            "100": [
                BilibiliDynamicPost(
                    id="dyn-3",
                    text="第三条动态",
                    url="https://t.bilibili.com/dyn-3",
                    rich_nodes=[BilibiliRichTextNode(kind="text", text="第三条动态")],
                    created_at=NOW_TS - 7200,
                    comment_oid=3003,
                    comment_type=17,
                ),
                BilibiliDynamicPost(
                    id="dyn-2",
                    text="第二条动态",
                    url="https://t.bilibili.com/dyn-2",
                    rich_nodes=[BilibiliRichTextNode(kind="text", text="第二条动态")],
                    created_at=NOW_TS - 7260,
                    comment_oid=3002,
                    comment_type=17,
                ),
                BilibiliDynamicPost(
                    id="dyn-1",
                    text="第一条动态",
                    url="https://t.bilibili.com/dyn-1",
                    rich_nodes=[BilibiliRichTextNode(kind="text", text="第一条动态")],
                    created_at=NOW_TS - 7320,
                    comment_oid=3001,
                    comment_type=17,
                ),
            ]
        }
        self.video_posts = {
            "100": [
                BilibiliVideoPost(id="BV3", title="第三个视频", url="https://www.bilibili.com/video/BV3", created_at=NOW_TS - 7200, comment_oid=2003),
                BilibiliVideoPost(id="BV2", title="第二个视频", url="https://www.bilibili.com/video/BV2", created_at=NOW_TS - 7260, comment_oid=2002),
                BilibiliVideoPost(id="BV1", title="第一个视频", url="https://www.bilibili.com/video/BV1", created_at=NOW_TS - 7320, comment_oid=2001),
            ]
        }
        self.live_status = {
            "100": BilibiliLiveStatus(
                is_live=False,
                title="直播已结束",
                room_id="123",
                url="https://live.bilibili.com/123",
            )
        }
        self.comments = {
            "video:2003": [
                BilibiliCommentPost(
                    id="9001",
                    author_uid="100",
                    author_name="测试账号",
                    text="这是旧评论",
                    created_at=100,
                    is_reply=False,
                    root_id="9001",
                )
            ]
        }
        self.sub_comments = {}
        self.comment_errors = {}

    async def get_user_name(self, uid: str) -> str:
        return self.names[uid]

    async def get_recent_dynamics(self, uid: str, stop_at_id: str | None):
        posts = self.dynamic_posts.get(uid, [])
        if stop_at_id is None:
            return posts[:1]
        result = []
        for post in posts:
            if post.id == stop_at_id:
                break
            result.append(post)
        return result

    async def get_recent_dynamics_with_status(self, uid: str, stop_at_id: str | None, max_items=None):
        posts = self.dynamic_posts.get(uid, [])
        if stop_at_id is None:
            result = posts[:1] if max_items is None else posts[: max(1, max_items)]
            return result, True
        result = []
        stop_found = False
        for post in posts:
            if post.id == stop_at_id:
                stop_found = True
                break
            result.append(post)
            if max_items is not None and len(result) >= max_items:
                break
        return result, stop_found

    async def get_recent_videos(self, uid: str, stop_at_id: str | None):
        posts = self.video_posts.get(uid, [])
        if stop_at_id is None:
            return posts[:1]
        result = []
        for post in posts:
            if post.id == stop_at_id:
                break
            result.append(post)
        return result

    async def get_recent_videos_with_status(self, uid: str, stop_at_id: str | None, max_items=None):
        posts = self.video_posts.get(uid, [])
        if stop_at_id is None:
            result = posts[:1] if max_items is None else posts[: max(1, max_items)]
            return result, True
        result = []
        stop_found = False
        for post in posts:
            if post.id == stop_at_id:
                stop_found = True
                break
            result.append(post)
            if max_items is not None and len(result) >= max_items:
                break
        return result, stop_found

    async def get_comment_resource_dynamics(self, uid: str, limit: int):
        self.comment_fetch_requests.append({"kind": "discover_dynamics", "uid": uid})
        return self.dynamic_posts.get(uid, [])[:limit]

    async def get_comment_resource_videos(self, uid: str, limit: int):
        self.comment_fetch_requests.append({"kind": "discover_videos", "uid": uid})
        return self.video_posts.get(uid, [])[:limit]

    async def get_comment_resource_owner_name(self, uid: str) -> str:
        self.comment_fetch_requests.append({"kind": "discover_owner", "uid": uid})
        return self.names[uid]

    async def get_live_status(self, uid: str):
        return self.live_status.get(uid)

    async def get_live_status_by_uid(self, uid: str):
        return self.live_status.get(uid)

    async def get_recent_comments(self, resource: BilibiliCommentResource, stop_comment_ids=None, stop_root_ids=None, max_pages=None):
        self.comment_fetch_requests.append(
            {
                "key": resource.key,
                "stop_comment_ids": list(stop_comment_ids or []),
                "stop_root_ids": list(stop_root_ids or []),
                "max_pages": max_pages,
            }
        )
        return list(self.comments.get(resource.key, []))

    async def get_comment_window(self, resource: BilibiliCommentResource, max_roots=20):
        self.comment_fetch_requests.append(
            {"kind": "comment_window", "key": resource.key, "max_roots": max_roots}
        )
        error = self.comment_errors.get(resource.key)
        if error is not None:
            raise error
        return list(self.comments.get(resource.key, []))

    async def get_recent_sub_comments(self, resource: BilibiliCommentResource, root_id: str, stop_comment_ids=None, max_pages=None):
        self.comment_fetch_requests.append(
            {
                "key": resource.key,
                "root_id": str(root_id),
                "stop_comment_ids": list(stop_comment_ids or []),
                "max_pages": max_pages,
            }
        )
        return list(self.sub_comments.get((resource.key, str(root_id)), []))


class FakeUserForLiveInfo:
    def __init__(self, payload) -> None:
        self.payload = payload

    async def get_live_info(self):
        return self.payload


class FakeUserForDynamics:
    def __init__(self, payload) -> None:
        self.payload = payload

    async def get_dynamics_new(self, offset=""):
        return self.payload


class ParsingGateway(BilibiliGateway):
    def __init__(self) -> None:
        super().__init__(
            request_client="aiohttp",
            credential_data={},
            comment_request_interval_seconds=0,
        )
        self.live_info_payload = {}
        self.dynamic_page_payload = None
        self.comment_module = None

    def _new_user(self, uid: str):
        if self.dynamic_page_payload is not None:
            return FakeUserForDynamics(self.dynamic_page_payload)
        return FakeUserForLiveInfo(self.live_info_payload)

    def _load_modules(self):
        if self.comment_module is not None:
            return object(), object(), self.comment_module
        return super()._load_modules()


class FakeCommentModule:
    class CommentResourceType:
        def __init__(self, value) -> None:
            self.value = value

    OrderType = types.SimpleNamespace(TIME="time")

    def __init__(self, pages) -> None:
        self.pages = pages
        self.calls: list[str] = []
        self.sub_comment_pages = {}

    async def get_comments_lazy(self, oid, type_, offset="", order=None, credential=None):
        self.calls.append(str(offset or ""))
        return self.pages.get(str(offset or ""), {})

    def Comment(self, oid, type_, rpid, credential=None):
        return FakeCommentObject(self, str(rpid))


class FakeCommentObject:
    def __init__(self, module: FakeCommentModule, root_id: str) -> None:
        self.module = module
        self.root_id = root_id

    async def get_sub_comments(self, page_index=1, page_size=10):
        self.module.calls.append(f"sub:{self.root_id}:{page_index}:{page_size}")
        return self.module.sub_comment_pages.get((self.root_id, page_index), {})


class BilibiliMonitorServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = FakeBilibiliGateway()
        self.service = BilibiliMonitorService(self.gateway)
        self.config = BilibiliPushConfig(
            enabled=True,
            poll_interval_seconds=120,
            task_gap_seconds=20.0,
            group_whitelist=["123456"],
            target_uids=["100"],
            push_dynamic=True,
            push_video=True,
            push_live=True,
            push_comment=True,
            request_client="aiohttp",
            credential_data={"sessdata": "test"},
        )

    async def _poll_uid(self, state):
        new_state = copy.deepcopy(state or {})
        uid_state_map = new_state.setdefault("uids", {})
        previous_uid_state = uid_state_map.get("100", {})
        snapshot = await self.service.fetch_uid_snapshot(
            config=self.config,
            uid="100",
            previous_state=previous_uid_state,
        )
        plan = self.service.plan_uid_deliveries(
            config=self.config,
            previous_state=previous_uid_state,
            snapshot=snapshot,
        )
        uid_state_map["100"] = plan.final_state
        return new_state, [delivery.notification for delivery in plan.deliveries]

    async def _poll_comments_uid(self, state):
        new_state = copy.deepcopy(state or {})
        uid_state_map = new_state.setdefault("uids", {})
        previous_uid_state = uid_state_map.get("100", {})
        resources = await self.service.discover_comment_resources("100", "测试账号")
        snapshot = await self.service.fetch_comment_snapshot(
            uid="100",
            author_name="测试账号",
            comment_resources=resources,
            previous_state=previous_uid_state,
        )
        plan = self.service.plan_comment_deliveries(
            config=self.config,
            previous_state=previous_uid_state,
            snapshot=snapshot,
        )
        uid_state_map["100"] = plan.final_state
        return new_state, [delivery.notification for delivery in plan.deliveries]

    def test_first_poll_only_initializes_state(self) -> None:
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            state, notifications = asyncio.run(self._poll_uid({}))

        self.assertEqual(notifications, [])
        self.assertEqual(state["uids"]["100"]["last_dynamic_id"], "dyn-3")
        self.assertFalse(state["uids"]["100"]["last_live_active"])

    def test_dynamic_notification_carries_all_card_fields(self) -> None:
        profile = BilibiliAuthorCardProfile(
            uid="100",
            name="资料昵称",
            avatar_url="https://i.example/avatar.png",
            follower=726000,
            fetched_at=NOW_TS,
        )
        stats = BilibiliEngagementStats(
            like_count=77,
            comment_count=6,
            forward_count=2,
        )
        additional = BilibiliAdditionalCard(
            kind="reserve",
            title="直播预约",
            subtitle="07-16 12:00 直播",
            url="https://www.bilibili.com/blackboard/live/activity",
        )
        forwarded = BilibiliForwardedContent(
            author_name="原作者",
            text="原动态正文",
        )
        post = BilibiliDynamicPost(
            id="dyn-card",
            text="新动态",
            url="https://t.bilibili.com/dyn-card",
            created_at=NOW_TS,
            stats=stats,
            additional_card=additional,
            forwarded=forwarded,
        )
        snapshot = BilibiliUidSnapshot(
            uid="100",
            author_name="测试账号",
            author_profile=profile,
            dynamics=[post],
        )

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            plan = self.service.plan_uid_deliveries(
                self.config,
                {
                    "last_dynamic_id": "old",
                    "last_dynamic_created_at": NOW_TS - 1,
                    "recent_dynamic_ids": ["old"],
                },
                snapshot,
            )

        notification = plan.deliveries[0].notification
        self.assertEqual(notification.content_id, "dyn-card")
        self.assertEqual(notification.published_at, NOW_TS)
        self.assertEqual(notification.author_profile, profile)
        self.assertEqual(notification.stats, stats)
        self.assertEqual(notification.additional_card, additional)
        self.assertEqual(notification.forwarded, forwarded)

    def test_second_poll_sends_all_unseen_dynamic_and_video_updates(self) -> None:
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            initial_state, _ = asyncio.run(self._poll_uid({}))

        self.gateway.dynamic_posts["100"] = [
            BilibiliDynamicPost(
                id="dyn-video-4",
                text="投稿了新视频",
                url="https://www.bilibili.com/video/BV4",
                title="第四个视频",
                cover_url="https://i0.hdslb.com/bfs/archive/video-cover-4.jpg",
                image_urls=["https://i0.hdslb.com/bfs/archive/video-cover-4.jpg"],
                video_bvid="BV4",
                created_at=NOW_TS - 60,
                is_video_dynamic=True,
                comment_oid=2004,
                comment_type=1,
            ),
            BilibiliDynamicPost(
                id="dyn-4",
                text="第四条动态",
                url="https://t.bilibili.com/dyn-4",
                rich_nodes=[BilibiliRichTextNode(kind="text", text="第四条动态")],
                created_at=NOW_TS - 120,
                comment_oid=3004,
                comment_type=17,
            ),
            *self.gateway.dynamic_posts["100"],
        ]
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            updated_state, notifications = asyncio.run(self._poll_uid(initial_state))

        self.assertEqual([item.kind for item in notifications], ["dynamic", "video"])
        self.assertEqual(notifications[1].video_bvid, "BV4")
        self.assertEqual(updated_state["uids"]["100"]["last_dynamic_id"], "dyn-video-4")

    def test_stale_cursor_rebuilds_baseline_without_replaying_history(self) -> None:
        stale_state = {
            "uids": {
                "100": {
                    "author_name": "测试账号",
                    "last_dynamic_id": "missing-dyn",
                    "last_live_active": False,
                    "comment_resources": {},
                }
            }
        }

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            updated_state, notifications = asyncio.run(self._poll_uid(stale_state))

        self.assertEqual(notifications, [])
        self.assertEqual(updated_state["uids"]["100"]["last_dynamic_id"], "dyn-3")

    def test_stale_cursor_only_replays_recent_posts_within_five_minutes(self) -> None:
        self.gateway.dynamic_posts["100"] = [
            BilibiliDynamicPost(
                id="dyn-video-4",
                text="投稿了新视频",
                url="https://www.bilibili.com/video/BV4",
                title="第四个视频",
                cover_url="https://i0.hdslb.com/bfs/archive/video-cover-4.jpg",
                image_urls=["https://i0.hdslb.com/bfs/archive/video-cover-4.jpg"],
                created_at=NOW_TS - 120,
                is_video_dynamic=True,
                comment_oid=2004,
                comment_type=1,
            ),
            BilibiliDynamicPost(
                id="dyn-4",
                text="第四条动态",
                url="https://t.bilibili.com/dyn-4",
                rich_nodes=[BilibiliRichTextNode(kind="text", text="第四条动态")],
                created_at=NOW_TS - 180,
                comment_oid=3004,
                comment_type=17,
            ),
            *self.gateway.dynamic_posts["100"],
        ]
        stale_state = {
            "uids": {
                "100": {
                    "author_name": "测试账号",
                    "last_dynamic_id": "missing-dyn",
                    "last_live_active": False,
                    "comment_resources": {},
                }
            }
        }

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            updated_state, notifications = asyncio.run(self._poll_uid(stale_state))

        self.assertEqual([item.kind for item in notifications], ["dynamic", "video"])
        self.assertEqual(updated_state["uids"]["100"]["last_dynamic_id"], "dyn-video-4")

    def test_persisted_state_prevents_replay_after_restart(self) -> None:
        persisted_state = {
            "uids": {
                "100": {
                    "author_name": "测试账号",
                    "last_dynamic_id": "dyn-3",
                    "recent_dynamic_ids": ["dyn-3", "dyn-2", "dyn-1"],
                    "last_live_active": False,
                    "comment_resources": {},
                }
            }
        }

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            updated_state, notifications = asyncio.run(
                self._poll_uid(persisted_state)
            )

        self.assertEqual(notifications, [])
        self.assertEqual(updated_state["uids"]["100"]["last_dynamic_id"], "dyn-3")

    def test_recent_dynamic_ids_prevent_replay_when_cursor_is_missing(self) -> None:
        self.gateway.dynamic_posts["100"] = [
            BilibiliDynamicPost(
                id="dyn-video-4",
                text="投稿了新视频",
                url="https://www.bilibili.com/video/BV4",
                title="第四个视频",
                cover_url="https://i0.hdslb.com/bfs/archive/video-cover-4.jpg",
                image_urls=["https://i0.hdslb.com/bfs/archive/video-cover-4.jpg"],
                created_at=NOW_TS - 120,
                is_video_dynamic=True,
            ),
            BilibiliDynamicPost(
                id="dyn-4",
                text="第四条动态",
                url="https://t.bilibili.com/dyn-4",
                rich_nodes=[BilibiliRichTextNode(kind="text", text="第四条动态")],
                created_at=NOW_TS - 180,
            ),
            *self.gateway.dynamic_posts["100"],
        ]
        stale_state = {
            "uids": {
                "100": {
                    "author_name": "测试账号",
                    "last_dynamic_id": "missing-dyn",
                    "recent_dynamic_ids": ["dyn-video-4", "dyn-4", "dyn-3", "dyn-2"],
                    "last_live_active": False,
                    "comment_resources": {},
                }
            }
        }

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            updated_state, notifications = asyncio.run(self._poll_uid(stale_state))

        self.assertEqual(notifications, [])
        self.assertEqual(updated_state["uids"]["100"]["last_dynamic_id"], "dyn-video-4")

    def test_live_notification_only_on_transition_to_live(self) -> None:
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            initial_state, _ = asyncio.run(self._poll_uid({}))

        self.gateway.dynamic_posts["100"].insert(
            0,
            BilibiliDynamicPost(
                id="dyn-live",
                text="【突击】直播开始了",
                url="https://live.bilibili.com/123?live_from=85002",
                rich_nodes=[BilibiliRichTextNode(kind="text", text="【突击】直播开始了")],
                image_urls=["https://i0.hdslb.com/live-cover.jpg"],
                created_at=NOW_TS - 60,
                is_live_room_dynamic=True,
            ),
        )
        self.gateway.live_status["100"] = BilibiliLiveStatus(
            is_live=True,
            title="今晚直播",
            room_id="123",
            url="https://live.bilibili.com/123",
        )
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            updated_state, notifications = asyncio.run(self._poll_uid(initial_state))

        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].kind, "live")
        self.assertTrue(updated_state["uids"]["100"]["last_live_active"])

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            repeated_state, repeated_notifications = asyncio.run(self._poll_uid(updated_state))
        self.assertEqual(repeated_notifications, [])
        self.assertTrue(repeated_state["uids"]["100"]["last_live_active"])

    def test_comment_notification_only_for_new_target_comments(self) -> None:
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            initial_state, notifications = asyncio.run(self._poll_comments_uid({}))

        self.assertEqual(notifications, [])
        self.assertIn(
            "video:2003",
            initial_state["uids"]["100"]["comment_resources"],
        )

        new_reply = BilibiliCommentPost(
            id="9002",
            author_uid="100",
            author_name="测试账号",
            text="这是新评论",
            created_at=200,
            is_reply=True,
            root_id="9001",
            parent_id="9001",
        )
        self.gateway.comments["video:2003"].insert(0, new_reply)
        self.gateway.sub_comments[("video:2003", "9001")] = [new_reply]

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            updated_state, updated_notifications = asyncio.run(self._poll_comments_uid(initial_state))

        self.assertEqual(len(updated_notifications), 1)
        self.assertEqual(updated_notifications[0].kind, "comment")
        self.assertEqual(updated_notifications[0].comment_action_text, "回复了评论")
        self.assertEqual(updated_notifications[0].comment_resource_kind, "视频")
        self.assertEqual(updated_notifications[0].comment_resource_title, "第三个视频")
        resource_state = updated_state["uids"]["100"]["comment_resources"]["video:2003"]
        self.assertEqual(resource_state["last_comment_id"], "9002")
        self.assertEqual(resource_state["roots"]["9001"]["last_reply_id"], "9002")

    def test_known_root_later_in_window_fetches_sub_comments_when_count_grows(self) -> None:
        self.gateway.comments["video:2003"] = [
            BilibiliCommentPost(
                id="9010",
                author_uid="200",
                author_name="路人",
                text="更新的一级评论",
                created_at=200,
                is_reply=False,
                root_id="9010",
            ),
            replace(self.gateway.comments["video:2003"][0], reply_count=2),
        ]
        previous_state = {
            "author_name": "测试账号",
            "comment_resources": {
                "video:2003": {
                    "initialized": True,
                    "last_comment_id": "9001",
                    "recent_comment_ids": ["9001"],
                    "recent_root_ids": ["9001"],
                    "roots": {
                        "9001": {
                            "reply_count": 1,
                            "recent_reply_ids": ["9000"],
                        }
                    },
                }
            },
        }
        resources = asyncio.run(
            self.service.discover_comment_resources("100", "测试账号")
        )
        asyncio.run(
            self.service.fetch_comment_snapshot(
                uid="100",
                author_name="测试账号",
                comment_resources=resources,
                previous_state=previous_state,
            )
        )

        sub_requests = [
            item
            for item in self.gateway.comment_fetch_requests
            if item.get("root_id") == "9001"
        ]
        self.assertEqual(len(sub_requests), 1)
        self.assertEqual(sub_requests[0]["stop_comment_ids"], ["9000"])

    def test_first_comment_snapshot_does_not_expand_sub_comment_trees(self) -> None:
        self.gateway.comments["video:2003"] = [
            replace(self.gateway.comments["video:2003"][0], reply_count=20)
        ]

        resources = asyncio.run(
            self.service.discover_comment_resources("100", "测试账号")
        )
        asyncio.run(
            self.service.fetch_comment_snapshot(
                uid="100",
                author_name="测试账号",
                comment_resources=resources,
                previous_state={},
            )
        )

        self.assertFalse(
            any("root_id" in item for item in self.gateway.comment_fetch_requests)
        )
        window_requests = [
            item
            for item in self.gateway.comment_fetch_requests
            if item.get("kind") == "comment_window"
        ]
        self.assertEqual(len(window_requests), 6)
        discovery_requests = [
            item
            for item in self.gateway.comment_fetch_requests
            if str(item.get("kind", "")).startswith("discover_")
        ]
        self.assertEqual(len(discovery_requests), 2)

    def test_failed_comment_resource_preserves_only_its_previous_state(self) -> None:
        failed_state = {
            "initialized": True,
            "last_comment_id": "old-failed",
            "recent_comment_ids": ["old-failed"],
            "recent_root_ids": ["old-failed"],
            "roots": {"old-failed": {"reply_count": 0}},
        }
        previous_state = {
            "author_name": "测试账号",
            "comment_resources": {"video:2003": failed_state},
        }
        self.gateway.comment_errors["video:2003"] = RuntimeError("resource closed")
        resources = asyncio.run(
            self.service.discover_comment_resources("100", "测试账号")
        )
        snapshot = asyncio.run(
            self.service.fetch_comment_snapshot(
                uid="100",
                author_name="测试账号",
                comment_resources=resources,
                previous_state=previous_state,
            )
        )
        plan = self.service.plan_comment_deliveries(
            self.config,
            previous_state,
            snapshot,
        )

        self.assertIn("video:2003", snapshot.resource_errors)
        self.assertEqual(
            plan.final_state["comment_resources"]["video:2003"],
            failed_state,
        )
        self.assertIn("video:2002", snapshot.comment_posts)

    def test_video_dynamic_comment_resource_is_not_built_twice(self) -> None:
        resources = self.service._build_comment_resources(
            owner_uid="100",
            owner_name="测试账号",
            dynamics=[
                BilibiliDynamicPost(
                    id="dyn-video-4",
                    text="投稿了新视频",
                    url="https://www.bilibili.com/video/BV4",
                    title="第四个视频",
                    created_at=NOW_TS - 60,
                    is_video_dynamic=True,
                    comment_oid=2004,
                    comment_type=1,
                )
            ],
            videos=[
                BilibiliVideoPost(
                    id="BV4",
                    title="第四个视频",
                    url="https://www.bilibili.com/video/BV4",
                    created_at=NOW_TS - 60,
                    comment_oid=2004,
                )
            ],
        )

        self.assertEqual([resource.key for resource in resources], ["video:2004"])

    def test_comment_dynamic_resources_ignore_old_pinned_order(self) -> None:
        dynamics = [
            BilibiliDynamicPost(
                id="dyn-old-pinned",
                text="旧置顶",
                url="https://t.bilibili.com/dyn-old-pinned",
                created_at=NOW_TS - 1000,
                comment_oid=3000,
                comment_type=17,
                is_pinned_dynamic=True,
            ),
            BilibiliDynamicPost(
                id="dyn-new-pinned",
                text="新置顶",
                url="https://t.bilibili.com/dyn-new-pinned",
                created_at=NOW_TS - 10,
                comment_oid=3004,
                comment_type=17,
                is_pinned_dynamic=True,
            ),
            BilibiliDynamicPost(
                id="dyn-new-3",
                text="新动态3",
                url="https://t.bilibili.com/dyn-new-3",
                created_at=NOW_TS - 30,
                comment_oid=3003,
                comment_type=17,
            ),
            BilibiliDynamicPost(
                id="dyn-new-2",
                text="新动态2",
                url="https://t.bilibili.com/dyn-new-2",
                created_at=NOW_TS - 20,
                comment_oid=3002,
                comment_type=17,
            ),
            BilibiliDynamicPost(
                id="dyn-new-1",
                text="新动态1",
                url="https://t.bilibili.com/dyn-new-1",
                created_at=NOW_TS - 40,
                comment_oid=3001,
                comment_type=17,
            ),
        ]

        selected = self.service._select_recent_comment_dynamics(dynamics)

        self.assertEqual(
            [post.id for post in selected],
            ["dyn-new-pinned", "dyn-new-2", "dyn-new-3"],
        )

    def test_comment_plan_deduplicates_duplicate_reply_ids(self) -> None:
        resource = BilibiliCommentResource(
            key="video:2003",
            owner_uid="100",
            owner_name="测试账号",
            resource_kind="video",
            oid=2003,
            type_value=1,
            title="第三个视频",
            url="https://www.bilibili.com/video/BV3",
        )
        reply = BilibiliCommentPost(
            id="9002",
            author_uid="100",
            author_name="测试账号",
            text="重复回复",
            created_at=200,
            is_reply=True,
            root_id="9001",
            parent_id="9001",
        )
        previous_state = {
            "author_name": "测试账号",
            "comment_resources": {
                "video:2003": {
                    "initialized": True,
                    "recent_comment_ids": [],
                    "recent_root_ids": ["9001"],
                    "roots": {"9001": {"recent_reply_ids": []}},
                }
            },
        }

        plan = self.service.plan_comment_deliveries(
            self.config,
            previous_state,
            BilibiliCommentSnapshot(
                uid="100",
                author_name="测试账号",
                comment_resources=[resource],
                comment_posts={"video:2003": [reply, reply]},
            ),
        )

        self.assertEqual([item.notification.text for item in plan.deliveries], ["重复回复"])

    def test_comment_plan_caches_non_target_comments_without_pushing_them(self) -> None:
        resource = BilibiliCommentResource(
            key="video:2003",
            owner_uid="100",
            owner_name="测试账号",
            resource_kind="video",
            oid=2003,
            type_value=1,
            title="第三个视频",
            url="https://www.bilibili.com/video/BV3",
        )
        previous_state = {
            "author_name": "测试账号",
            "comment_resources": {
                "video:2003": {
                    "initialized": True,
                    "recent_comment_ids": [],
                    "recent_root_ids": [],
                    "roots": {},
                }
            },
        }
        comments = [
            BilibiliCommentPost(
                id="9001",
                author_uid="200",
                author_name="普通用户",
                text="普通用户评论",
                created_at=200,
                is_reply=False,
                root_id="9001",
                reply_count=1,
            ),
            BilibiliCommentPost(
                id="9002",
                author_uid="100",
                author_name="测试账号",
                text="目标账号回复",
                created_at=300,
                is_reply=True,
                root_id="9001",
                parent_id="9001",
            ),
        ]

        plan = self.service.plan_comment_deliveries(
            self.config,
            previous_state,
            BilibiliCommentSnapshot(
                uid="100",
                author_name="测试账号",
                comment_resources=[resource],
                comment_posts={"video:2003": comments},
            ),
        )

        self.assertEqual(
            [item.notification.text for item in plan.deliveries],
            ["目标账号回复"],
        )
        delivery_state = plan.deliveries[0].uid_state["comment_resources"]["video:2003"]
        self.assertEqual(delivery_state["recent_comment_ids"], ["9002", "9001"])
        self.assertEqual(
            plan.final_state["comment_resources"]["video:2003"]["roots"]["9001"]["reply_count"],
            1,
        )

    def test_comment_plan_orders_replies_and_updates_root_cache(self) -> None:
        resource = BilibiliCommentResource(
            key="video:2003",
            owner_uid="100",
            owner_name="测试账号",
            resource_kind="video",
            oid=2003,
            type_value=1,
            title="第三个视频",
            url="https://www.bilibili.com/video/BV3",
        )
        previous_state = {
            "author_name": "测试账号",
            "comment_resources": {
                "video:2003": {
                    "initialized": True,
                    "recent_comment_ids": [],
                    "recent_root_ids": ["9001"],
                    "roots": {"9001": {"recent_reply_ids": []}},
                }
            },
        }
        comments = [
            BilibiliCommentPost(
                id="9003",
                author_uid="100",
                author_name="测试账号",
                text="较晚回复",
                created_at=300,
                is_reply=True,
                root_id="9001",
                parent_id="9001",
            ),
            BilibiliCommentPost(
                id="9002",
                author_uid="100",
                author_name="测试账号",
                text="较早回复",
                created_at=200,
                is_reply=True,
                root_id="9001",
                parent_id="9001",
            ),
        ]

        plan = self.service.plan_comment_deliveries(
            self.config,
            previous_state,
            BilibiliCommentSnapshot(
                uid="100",
                author_name="测试账号",
                comment_resources=[resource],
                comment_posts={"video:2003": comments},
            ),
        )

        self.assertEqual(
            [item.notification.text for item in plan.deliveries],
            ["较早回复", "较晚回复"],
        )
        root_state = plan.final_state["comment_resources"]["video:2003"]["roots"]["9001"]
        self.assertEqual(root_state["last_reply_id"], "9003")
        self.assertEqual(root_state["recent_reply_ids"], ["9003", "9002"])

    def test_comment_plan_prunes_inactive_resources(self) -> None:
        active_resource = BilibiliCommentResource(
            key="video:2003",
            owner_uid="100",
            owner_name="测试账号",
            resource_kind="video",
            oid=2003,
            type_value=1,
            title="第三个视频",
            url="https://www.bilibili.com/video/BV3",
        )
        previous_state = {
            "author_name": "测试账号",
            "comment_resources": {
                "video:old": {
                    "initialized": True,
                    "recent_comment_ids": ["1"],
                }
            },
        }

        plan = self.service.plan_comment_deliveries(
            self.config,
            previous_state,
            BilibiliCommentSnapshot(
                uid="100",
                author_name="测试账号",
                comment_resources=[active_resource],
                comment_posts={"video:2003": []},
            ),
        )

        self.assertEqual(list(plan.final_state["comment_resources"]), ["video:2003"])

    def test_second_poll_delivers_two_reserve_dynamics_in_order(self) -> None:
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            initial_state, _ = asyncio.run(self._poll_uid({}))

        self.gateway.dynamic_posts["100"] = [
            BilibiliDynamicPost(
                id="dyn-reserve-2",
                text="今晚再约一次",
                url="https://live.bilibili.com/blackboard/reserve-2",
                rich_nodes=[BilibiliRichTextNode(kind="text", text="今晚再约一次")],
                created_at=NOW_TS - 30,
                comment_oid=3012,
                comment_type=17,
            ),
            BilibiliDynamicPost(
                id="dyn-reserve-1",
                text="明晚先约一个",
                url="https://live.bilibili.com/blackboard/reserve-1",
                rich_nodes=[BilibiliRichTextNode(kind="text", text="明晚先约一个")],
                created_at=NOW_TS - 60,
                comment_oid=3011,
                comment_type=17,
            ),
            *self.gateway.dynamic_posts["100"],
        ]

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            updated_state, notifications = asyncio.run(self._poll_uid(initial_state))

        self.assertEqual(
            [notification.url for notification in notifications if notification.kind == "dynamic"],
            [
                "https://live.bilibili.com/blackboard/reserve-1",
                "https://live.bilibili.com/blackboard/reserve-2",
            ],
        )
        self.assertEqual(updated_state["uids"]["100"]["last_dynamic_id"], "dyn-reserve-2")


class BilibiliConfigParsingTest(unittest.TestCase):
    def test_card_rendering_defaults_on_and_can_be_disabled(self) -> None:
        self.assertTrue(build_bilibili_push_config({}).render_bilibili_cards)
        self.assertFalse(
            build_bilibili_push_config(
                {"render_bilibili_cards": False}
            ).render_bilibili_cards
        )

    def test_build_config_falls_back_when_poll_interval_is_invalid(self) -> None:
        config = build_bilibili_push_config(
            {
                "enabled": True,
                "poll_interval_seconds": "abc",
            }
        )

        self.assertEqual(config.poll_interval_seconds, 300)
        self.assertEqual(config.task_gap_seconds, 20.0)

    def test_comment_polling_is_disabled_by_default(self) -> None:
        config = build_bilibili_push_config({})

        self.assertFalse(config.push_comment)

    def test_normalize_bilibili_uid_rejects_non_digit_value(self) -> None:
        with self.assertRaises(ValueError):
            normalize_bilibili_uid("abc123")


class BilibiliParsingTest(unittest.TestCase):
    def test_get_video_engagement_stats_uses_video_detail_stat_fields(self) -> None:
        created_videos = []

        class FakeVideo:
            def __init__(self, **kwargs):
                created_videos.append(kwargs)

            async def get_info(self):
                return {
                    "stat": {
                        "like": 70696,
                        "reply": 5468,
                        "share": 6081,
                    }
                }

        gateway = BilibiliGateway()
        gateway._load_video_module = lambda: types.SimpleNamespace(Video=FakeVideo)

        stats = asyncio.run(gateway.get_video_engagement_stats("BV1SdXWB2Enp"))

        self.assertEqual(
            stats,
            BilibiliEngagementStats(
                like_count=70696,
                comment_count=5468,
                forward_count=6081,
            ),
        )
        self.assertEqual(created_videos, [{"bvid": "BV1SdXWB2Enp"}])

    def test_get_video_engagement_stats_rejects_missing_stat_payload(self) -> None:
        class FakeVideo:
            async def get_info(self):
                return {"title": "缺少统计字段的视频"}

        class FakeVideoModule:
            @staticmethod
            def Video(**_kwargs):
                return FakeVideo()

        gateway = BilibiliGateway()
        gateway._load_video_module = lambda: FakeVideoModule

        with self.assertRaises(RuntimeError):
            asyncio.run(gateway.get_video_engagement_stats("BV1SdXWB2Enp"))

    def test_get_user_card_profile_combines_three_user_apis(self) -> None:
        class FakeUser:
            async def get_user_info(self):
                return {
                    "name": "测试账号",
                    "face": "//i.example/avatar.png",
                    "pendant": {"image": "https://i.example/pendant.png"},
                }

            async def get_relation_info(self):
                return {"following": 23, "follower": 726000}

            async def get_up_stat(self):
                return {"likes": 18807000}

        gateway = BilibiliGateway()
        gateway._new_user = lambda _uid: FakeUser()

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            profile = asyncio.run(gateway.get_user_card_profile("100"))

        self.assertEqual(
            profile,
            BilibiliAuthorCardProfile(
                uid="100",
                name="测试账号",
                avatar_url="https://i.example/avatar.png",
                pendant_url="https://i.example/pendant.png",
                total_likes=18807000,
                following=23,
                follower=726000,
                fetched_at=NOW_TS,
            ),
        )

    def setUp(self) -> None:
        self.gateway = ParsingGateway()

    def _dynamic_push_config(self) -> BilibiliPushConfig:
        return BilibiliPushConfig(
            enabled=True,
            poll_interval_seconds=120,
            task_gap_seconds=20.0,
            group_whitelist=["123456"],
            target_uids=["100"],
            push_dynamic=True,
            push_video=True,
            push_live=False,
            push_comment=False,
            request_client="aiohttp",
            credential_data={"sessdata": "test"},
        )

    def test_parse_live_rcmd_dynamic_extracts_title_cover_and_link(self) -> None:
        item = {
            "id_str": "dyn-live",
            "modules": {
                "module_dynamic": {
                    "major": {
                        "live_rcmd": {
                            "content": json.dumps(
                                {
                                    "live_play_info": {
                                        "title": "【突击】先看成龙历险记然后洛克王国世界！",
                                        "link": "https://live.bilibili.com/22632424",
                                        "cover": "https://i0.hdslb.com/live-cover.jpg",
                                    }
                                }
                            )
                        }
                    }
                }
            },
        }

        post = self.gateway._parse_dynamic_post(item)

        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.url, "https://live.bilibili.com/22632424")
        self.assertIn("先看成龙历险记然后洛克王国世界", post.text)
        self.assertEqual(post.image_urls, ["https://i0.hdslb.com/live-cover.jpg"])
        self.assertTrue(post.is_live_room_dynamic)

    def test_parse_forward_dynamic_includes_original_content(self) -> None:
        item = {
            "id_str": "dyn-forward",
            "modules": {
                "module_dynamic": {
                    "desc": {"text": ""},
                }
            },
            "orig": {
                "modules": {
                    "module_author": {"name": "A-SOUL_Official"},
                    "module_dynamic": {
                        "desc": {
                            "text": "Hello小伙伴们大家好~3.30-4.5的日程表来咯！"
                        },
                        "major": {
                            "draw": {
                                "items": [
                                    {"src": "https://i0.hdslb.com/forward-preview.jpg"}
                                ]
                            }
                        },
                    },
                },
            },
        }

        post = self.gateway._parse_dynamic_post(item)

        self.assertIsNotNone(post)
        assert post is not None
        self.assertIn("┈" * 24 + "\n转发自 A-SOUL_Official", post.text)
        self.assertIn("转发自 A-SOUL_Official", post.text)
        self.assertIn("Hello小伙伴们大家好", post.text)
        self.assertIn("https://i0.hdslb.com/forward-preview.jpg", post.image_urls)

    def test_parse_reserve_dynamic_includes_reservation_card(self) -> None:
        item = {
            "id_str": "dyn-reserve",
            "modules": {
                "module_dynamic": {
                    "desc": {
                        "text": "所以明晚电台跟大家见面好不好呀奶淇琳"
                    },
                    "additional": {
                        "reserve": {
                            "title": "直播预约：【突击/电台】一起聊聊天~",
                            "desc1": {"text": "明天 20:00 直播"},
                            "desc2": {"text": "3191人预约"},
                            "jump_url": "https://live.bilibili.com/blackboard/reserve",
                        }
                    },
                }
            },
        }

        post = self.gateway._parse_dynamic_post(item)

        self.assertIsNotNone(post)
        assert post is not None
        self.assertIn("明晚电台", post.text)
        self.assertIn("直播预约：【突击/电台】一起聊聊天~", post.text)
        self.assertIn("明天 20:00 直播", post.text)
        self.assertEqual(post.url, "https://t.bilibili.com/dyn-reserve")
        self.assertEqual(
            post.additional_card.url,
            "https://live.bilibili.com/blackboard/reserve",
        )

    def test_parse_dynamic_extracts_structured_card_metadata(self) -> None:
        item = {
            "id_str": "dyn-card",
            "basic": {"jump_url": "javascript:alert(1)"},
            "modules": {
                "module_author": {
                    "name": "贝拉kira",
                    "face": "//i0.hdslb.com/avatar.jpg",
                    "pendant": {"image": "https://i0.hdslb.com/pendant.png"},
                    "pub_ts": NOW_TS,
                },
                "module_stat": {
                    "like": {"count": 77},
                    "comment": {"count": 6},
                    "forward": {"count": 2},
                },
                "module_dynamic": {
                    "desc": {"text": "巨龙腾飞！"},
                    "major": {
                        "opus": {
                            "pics": [
                                {"url": f"https://i0.hdslb.com/{index}.jpg"}
                                for index in range(10)
                            ]
                        }
                    },
                    "additional": {
                        "reserve": {
                            "title": "直播预约：【突击】过终末地1.4主线！",
                            "desc1": {"text": "07-16 12:00 直播"},
                            "desc2": {"text": "60人预约"},
                            "jump_url": "https://live.bilibili.com/blackboard/reserve",
                        }
                    },
                },
            },
        }

        post = self.gateway._parse_dynamic_post(item)

        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.url, "https://t.bilibili.com/dyn-card")
        self.assertEqual(post.author.name, "贝拉kira")
        self.assertEqual(post.author.avatar_url, "https://i0.hdslb.com/avatar.jpg")
        self.assertEqual(post.author.pendant_url, "https://i0.hdslb.com/pendant.png")
        self.assertEqual(post.stats.like_count, 77)
        self.assertEqual(post.stats.comment_count, 6)
        self.assertEqual(post.stats.forward_count, 2)
        self.assertEqual(len(post.image_urls), 9)
        self.assertEqual(post.additional_card.kind, "reserve")
        self.assertIn("终末地1.4主线", post.additional_card.title)
        self.assertEqual(post.additional_card.subtitle, "07-16 12:00 直播 · 60人预约")

    def test_parse_video_dynamic_extracts_bvid(self) -> None:
        item = {
            "id_str": "dyn-video",
            "modules": {
                "module_dynamic": {
                    "major": {
                        "archive": {
                            "bvid": "BV1SdXWB2Enp",
                            "title": "测试视频",
                            "jump_url": "https://www.bilibili.com/video/BV1SdXWB2Enp",
                        }
                    }
                }
            },
        }

        post = self.gateway._parse_dynamic_post(item)

        self.assertIsNotNone(post)
        assert post is not None
        self.assertTrue(post.is_video_dynamic)
        self.assertEqual(post.video_bvid, "BV1SdXWB2Enp")

    def test_parse_dynamic_rich_text_preserves_safe_links_and_emotes(self) -> None:
        item = {
            "id_str": "dyn-rich",
            "modules": {
                "module_dynamic": {
                    "desc": {
                        "rich_text_nodes": [
                            {
                                "type": "RICH_TEXT_NODE_TYPE_TOPIC",
                                "text": "#测试话题#",
                                "jump_url": "https://search.bilibili.com/all?keyword=test",
                            },
                            {
                                "type": "RICH_TEXT_NODE_TYPE_EMOJI",
                                "text": "[星星眼]",
                                "emoji": {"icon_url": "//i.example/emoji.png"},
                            },
                            {
                                "type": "RICH_TEXT_NODE_TYPE_WEB",
                                "text": "危险",
                                "jump_url": "javascript:alert(1)",
                            },
                        ]
                    }
                }
            },
        }

        post = self.gateway._parse_dynamic_post(item)

        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.rich_nodes[0].kind, "link")
        self.assertEqual(
            post.rich_nodes[0].url,
            "https://search.bilibili.com/all?keyword=test",
        )
        self.assertEqual(post.rich_nodes[1].kind, "emoji")
        self.assertEqual(post.rich_nodes[1].image_url, "https://i.example/emoji.png")
        self.assertEqual(post.rich_nodes[2].kind, "text")
        self.assertEqual(post.rich_nodes[2].url, "")

    def test_parse_forward_dynamic_preserves_structured_original(self) -> None:
        item = {
            "id_str": "dyn-forward-card",
            "modules": {
                "module_dynamic": {"desc": {"text": "转发一下"}},
            },
            "orig": {
                "modules": {
                    "module_author": {
                        "name": "A-SOUL_Official",
                        "face": "https://i0.hdslb.com/original-avatar.jpg",
                    },
                    "module_dynamic": {
                        "desc": {"text": "原动态正文"},
                        "major": {
                            "draw": {
                                "items": [
                                    {"src": "https://i0.hdslb.com/original.jpg"}
                                ]
                            }
                        },
                    },
                }
            },
        }

        post = self.gateway._parse_dynamic_post(item)

        self.assertIsNotNone(post)
        assert post is not None
        self.assertIsNotNone(post.forwarded)
        assert post.forwarded is not None
        self.assertEqual(post.forwarded.author_name, "A-SOUL_Official")
        self.assertEqual(post.forwarded.text, "原动态正文")
        self.assertEqual(
            post.forwarded.image_urls,
            ["https://i0.hdslb.com/original.jpg"],
        )

    def test_pinned_dynamic_newer_than_cursor_is_delivered_even_after_five_minutes(self) -> None:
        snapshot = BilibiliUidSnapshot(
            uid="100",
            author_name="测试账号",
            dynamics=[
                BilibiliDynamicPost(
                    id="dyn-2",
                    text="刚发出就被置顶的新动态",
                    url="https://t.bilibili.com/dyn-2",
                    created_at=NOW_TS - (6 * 60),
                    is_pinned_dynamic=True,
                ),
                BilibiliDynamicPost(
                    id="dyn-1",
                    text="当前已处理游标",
                    url="https://t.bilibili.com/dyn-1",
                    created_at=NOW_TS - (10 * 60),
                ),
            ],
        )
        previous_state = {
            "author_name": "测试账号",
            "last_dynamic_id": "dyn-1",
            "recent_dynamic_ids": ["dyn-1"],
        }

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            plan = BilibiliMonitorService(self.gateway).plan_uid_deliveries(
                self._dynamic_push_config(),
                previous_state,
                snapshot,
            )

        self.assertEqual([delivery.notification.text for delivery in plan.deliveries], ["刚发出就被置顶的新动态"])
        self.assertEqual(plan.final_state["last_dynamic_id"], "dyn-2")

    def test_gateway_marks_pinned_dynamic_without_filtering_it(self) -> None:
        self.gateway.dynamic_page_payload = {
            "items": [
                {
                    "id_str": "dyn-2",
                    "basic": {
                        "comment_id_str": "3002",
                        "comment_type": 17,
                    },
                    "modules": {
                        "module_tag": {"text": "置顶"},
                        "module_author": {
                            "pub_ts": NOW_TS - (6 * 60),
                        },
                        "module_dynamic": {
                            "desc": {"text": "很久之前的老置顶"},
                        },
                    },
                },
                {
                    "id_str": "dyn-1",
                    "basic": {
                        "comment_id_str": "3001",
                        "comment_type": 17,
                    },
                    "modules": {
                        "module_author": {
                            "pub_ts": NOW_TS - 30,
                        },
                        "module_dynamic": {
                            "desc": {"text": "当前已处理游标"},
                        },
                    },
                },
            ]
        }

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            posts, stop_found = asyncio.run(
                self.gateway.get_recent_dynamics_with_status(
                    "100",
                    stop_at_id="dyn-1",
                )
            )

        self.assertTrue(stop_found)
        self.assertEqual([post.id for post in posts], ["dyn-2"])
        self.assertTrue(posts[0].is_pinned_dynamic)

    def test_pinned_cursor_does_not_block_newer_post_appearing_after_it(self) -> None:
        """当置顶动态是游标且排在 API 返回列表第一位时，排在它后面的更新非置顶动态不应被遗漏。"""
        snapshot = BilibiliUidSnapshot(
            uid="100",
            author_name="测试账号",
            dynamics=[
                BilibiliDynamicPost(
                    id="dyn-pinned",
                    text="被置顶的游标",
                    url="https://t.bilibili.com/dyn-pinned",
                    created_at=NOW_TS - 120,
                    is_pinned_dynamic=True,
                ),
                BilibiliDynamicPost(
                    id="dyn-new",
                    text="发布在置顶之后的新动态",
                    url="https://t.bilibili.com/dyn-new",
                    created_at=NOW_TS - 60,
                ),
            ],
        )
        previous_state = {
            "author_name": "测试账号",
            "last_dynamic_id": "dyn-pinned",
            "last_dynamic_created_at": NOW_TS - 120,
            "recent_dynamic_ids": ["dyn-pinned"],
        }

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            plan = BilibiliMonitorService(self.gateway).plan_uid_deliveries(
                self._dynamic_push_config(),
                previous_state,
                snapshot,
            )

        self.assertEqual(
            [d.notification.text for d in plan.deliveries],
            ["发布在置顶之后的新动态"],
        )

    def test_stale_pinned_cursor_does_not_replay_days_of_history(self) -> None:
        snapshot = BilibiliUidSnapshot(
            uid="100",
            author_name="测试账号",
            dynamics=[
                BilibiliDynamicPost(
                    id="dyn-recent",
                    text="最近的新动态",
                    url="https://t.bilibili.com/dyn-recent",
                    created_at=NOW_TS - 60,
                ),
                BilibiliDynamicPost(
                    id="dyn-old-missed",
                    text="几天前遗漏的旧动态",
                    url="https://t.bilibili.com/dyn-old-missed",
                    created_at=NOW_TS - (3 * 24 * 60 * 60),
                ),
                BilibiliDynamicPost(
                    id="dyn-pinned",
                    text="几天前被置顶的游标",
                    url="https://t.bilibili.com/dyn-pinned",
                    created_at=NOW_TS - (4 * 24 * 60 * 60),
                    is_pinned_dynamic=True,
                ),
            ],
        )
        previous_state = {
            "author_name": "测试账号",
            "last_dynamic_id": "dyn-pinned",
            "last_dynamic_created_at": NOW_TS - (4 * 24 * 60 * 60),
            "recent_dynamic_ids": ["dyn-pinned"],
        }

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            plan = BilibiliMonitorService(self.gateway).plan_uid_deliveries(
                self._dynamic_push_config(),
                previous_state,
                snapshot,
            )

        self.assertEqual(
            [d.notification.text for d in plan.deliveries],
            ["最近的新动态"],
        )
        self.assertEqual(plan.final_state["last_dynamic_id"], "dyn-recent")

    def test_parse_comment_post_preserves_images_and_emotes_without_text(self) -> None:
        post = self.gateway._parse_comment_post(
            {
                "rpid_str": "99001",
                "ctime": NOW_TS,
                "parent": 0,
                "member": {
                    "mid": "672328094",
                    "uname": "乃琳Queen",
                },
                "content": {
                    "message": "",
                    "pictures": [
                        {"img_src": "//i0.hdslb.com/comment-a.png"},
                        {"url": "https://i0.hdslb.com/comment-b.png"},
                    ],
                    "emote": {
                        "1": {"url": "https://i0.hdslb.com/emote-a.png"},
                        "2": {"icon_url": "https://i0.hdslb.com/emote-b.png"},
                    },
                },
            }
        )

        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.text, "")
        self.assertEqual(
            post.image_urls,
            [
                "https://i0.hdslb.com/comment-a.png",
                "https://i0.hdslb.com/comment-b.png",
                "https://i0.hdslb.com/emote-a.png",
                "https://i0.hdslb.com/emote-b.png",
            ],
        )
        self.assertFalse(post.is_reply)

    def test_get_recent_comments_pages_until_known_comment(self) -> None:
        self.gateway.comment_module = FakeCommentModule(
            {
                "": {
                    "replies": [
                        {
                            "rpid_str": "9004",
                            "ctime": 104,
                            "parent": 0,
                            "member": {"mid": "100", "uname": "测试账号"},
                            "content": {"message": "第四条"},
                        },
                        {
                            "rpid_str": "9003",
                            "ctime": 103,
                            "parent": 0,
                            "member": {"mid": "100", "uname": "测试账号"},
                            "content": {"message": "第三条"},
                        },
                    ],
                    "cursor": {"pagination_reply": {"next_offset": "page-2"}},
                },
                "page-2": {
                    "replies": [
                        {
                            "rpid_str": "9002",
                            "ctime": 102,
                            "parent": 0,
                            "rcount": 4,
                            "member": {"mid": "100", "uname": "测试账号"},
                            "content": {"message": "第二条"},
                        },
                        {
                            "rpid_str": "9001",
                            "ctime": 101,
                            "parent": 0,
                            "member": {"mid": "100", "uname": "测试账号"},
                            "content": {"message": "第一条"},
                        },
                    ],
                    "cursor": {"pagination_reply": {"next_offset": "page-3"}},
                },
            }
        )

        comments = asyncio.run(
            self.gateway.get_recent_comments(
                BilibiliCommentResource(
                    key="video:2003",
                    owner_uid="100",
                    owner_name="测试账号",
                    resource_kind="video",
                    oid=2003,
                    type_value=1,
                    title="第三个视频",
                    url="https://www.bilibili.com/video/BV3",
                ),
                stop_comment_ids=["9002"],
                max_pages=5,
            )
        )

        self.assertEqual(
            [comment.id for comment in comments],
            ["9004", "9003", "9002"],
        )
        self.assertEqual(comments[-1].reply_count, 4)
        self.assertEqual(self.gateway.comment_module.calls, ["", "page-2"])

    def test_comment_window_keeps_only_first_twenty_roots(self) -> None:
        self.gateway.comment_module = FakeCommentModule(
            {
                "": {
                    "replies": [
                        {
                            "rpid_str": str(10_000 - index),
                            "ctime": 10_000 - index,
                            "parent": 0,
                            "member": {"mid": "100", "uname": "测试账号"},
                            "content": {"message": f"第 {index} 条"},
                        }
                        for index in range(21)
                    ]
                }
            }
        )

        comments = asyncio.run(
            self.gateway.get_comment_window(
                BilibiliCommentResource(
                    key="video:2003",
                    owner_uid="100",
                    owner_name="测试账号",
                    resource_kind="video",
                    oid=2003,
                    type_value=1,
                    title="第三个视频",
                    url="https://www.bilibili.com/video/BV3",
                )
            )
        )

        self.assertEqual(len(comments), 20)
        self.assertNotIn("9980", [comment.id for comment in comments])
        self.assertEqual(self.gateway.comment_module.calls, [""])

    def test_get_recent_sub_comments_stops_at_known_reply(self) -> None:
        self.gateway.comment_module = FakeCommentModule({})
        self.gateway.comment_module.sub_comment_pages = {
            ("9001", 1): {
                "replies": [
                    {
                        "rpid_str": "9004",
                        "ctime": 104,
                        "parent": 9001,
                        "root": 9001,
                        "member": {"mid": "100", "uname": "测试账号"},
                        "content": {"message": "新楼中楼"},
                    },
                    {
                        "rpid_str": "9002",
                        "ctime": 102,
                        "parent": 9001,
                        "root": 9001,
                        "member": {"mid": "100", "uname": "测试账号"},
                        "content": {"message": "已知楼中楼"},
                    },
                ]
            },
            ("9001", 2): {
                "replies": [
                    {
                        "rpid_str": "9000",
                        "ctime": 100,
                        "parent": 9001,
                        "root": 9001,
                        "member": {"mid": "100", "uname": "测试账号"},
                        "content": {"message": "不应再翻页"},
                    }
                ]
            },
        }

        comments = asyncio.run(
            self.gateway.get_recent_sub_comments(
                BilibiliCommentResource(
                    key="video:2003",
                    owner_uid="100",
                    owner_name="测试账号",
                    resource_kind="video",
                    oid=2003,
                    type_value=1,
                    title="第三个视频",
                    url="https://www.bilibili.com/video/BV3",
                ),
                root_id="9001",
                stop_comment_ids=["9002"],
                max_pages=3,
            )
        )

        self.assertEqual([comment.id for comment in comments], ["9004"])
        self.assertEqual(comments[0].root_id, "9001")
        self.assertEqual(comments[0].parent_id, "9001")
        self.assertEqual(
            self.gateway.comment_module.calls,
            ["sub:9001:1:20"],
        )

    def test_get_live_status_prefers_room_info_title(self) -> None:
        self.gateway.live_info_payload = {
            "title": "虚拟偶像团体A-SOUL 所属艺人",
            "room_info": {
                "title": "【突击】先看成龙历险记然后洛克王国世界！",
                "room_id": 22632424,
                "live_status": 1,
                "cover": "https://i0.hdslb.com/live-room-cover.jpg",
            },
        }

        status = asyncio.run(self.gateway.get_live_status("672353429"))

        self.assertIsNotNone(status)
        assert status is not None
        self.assertTrue(status.is_live)
        self.assertEqual(status.room_id, "22632424")
        self.assertEqual(status.title, "【突击】先看成龙历险记然后洛克王国世界！")
        self.assertEqual(status.url, "https://live.bilibili.com/22632424")
        self.assertEqual(status.cover_url, "https://i0.hdslb.com/live-room-cover.jpg")

    def test_get_live_status_supports_live_room_status_shape(self) -> None:
        self.gateway.live_info_payload = {
            "official": {
                "title": "虚拟偶像团体A-SOUL 所属艺人",
            },
            "live_room": {
                "roomStatus": 1,
                "liveStatus": 1,
                "url": "https://live.bilibili.com/22632424?broadcast_type=0&is_room_feed=1",
                "title": "【突击】和贝拉一起洛克王国世界！",
                "cover": "https://i0.hdslb.com/bfs/live/new_room_cover/11a9c6e355c7af3b6b62e6a72ef4943ad545c827.jpg",
                "roomid": 22632424,
            },
        }

        status = asyncio.run(self.gateway.get_live_status("672353429"))

        self.assertIsNotNone(status)
        assert status is not None
        self.assertTrue(status.is_live)
        self.assertEqual(status.room_id, "22632424")
        self.assertEqual(status.title, "【突击】和贝拉一起洛克王国世界！")
        self.assertEqual(
            status.url,
            "https://live.bilibili.com/22632424?broadcast_type=0&is_room_feed=1",
        )
