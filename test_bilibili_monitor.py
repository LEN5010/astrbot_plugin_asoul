import asyncio
import unittest

from asoul_bilibili import (
    BilibiliCommentPost,
    BilibiliCommentResource,
    BilibiliDynamicPost,
    BilibiliLiveStatus,
    BilibiliMonitorService,
    BilibiliPushConfig,
    BilibiliRichTextNode,
    BilibiliVideoPost,
)


class FakeBilibiliGateway:
    def __init__(self) -> None:
        self.names = {"100": "测试账号"}
        self.dynamic_posts = {
            "100": [
                BilibiliDynamicPost(
                    id="dyn-3",
                    text="第三条动态",
                    url="https://t.bilibili.com/dyn-3",
                    rich_nodes=[BilibiliRichTextNode(kind="text", text="第三条动态")],
                    comment_oid=3003,
                    comment_type=17,
                ),
                BilibiliDynamicPost(
                    id="dyn-2",
                    text="第二条动态",
                    url="https://t.bilibili.com/dyn-2",
                    rich_nodes=[BilibiliRichTextNode(kind="text", text="第二条动态")],
                    comment_oid=3002,
                    comment_type=17,
                ),
                BilibiliDynamicPost(
                    id="dyn-1",
                    text="第一条动态",
                    url="https://t.bilibili.com/dyn-1",
                    rich_nodes=[BilibiliRichTextNode(kind="text", text="第一条动态")],
                    comment_oid=3001,
                    comment_type=17,
                ),
            ]
        }
        self.video_posts = {
            "100": [
                BilibiliVideoPost(id="BV3", title="第三个视频", url="https://www.bilibili.com/video/BV3", comment_oid=2003),
                BilibiliVideoPost(id="BV2", title="第二个视频", url="https://www.bilibili.com/video/BV2", comment_oid=2002),
                BilibiliVideoPost(id="BV1", title="第一个视频", url="https://www.bilibili.com/video/BV1", comment_oid=2001),
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
                )
            ]
        }

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

    async def get_latest_dynamics(self, uid: str, limit: int):
        return self.dynamic_posts.get(uid, [])[:limit]

    async def get_latest_videos(self, uid: str, limit: int):
        return self.video_posts.get(uid, [])[:limit]

    async def get_live_status(self, uid: str):
        return self.live_status.get(uid)

    async def get_recent_comments(self, resource: BilibiliCommentResource):
        return list(self.comments.get(resource.key, []))


class BilibiliMonitorServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = FakeBilibiliGateway()
        self.service = BilibiliMonitorService(self.gateway)
        self.config = BilibiliPushConfig(
            enabled=True,
            poll_interval_seconds=120,
            group_whitelist=["123456"],
            target_uids=["100"],
            push_dynamic=True,
            push_video=True,
            push_live=True,
            push_comment=True,
            request_client="aiohttp",
            credential_data={"sessdata": "test"},
        )

    def test_first_poll_only_initializes_state(self) -> None:
        state, notifications = asyncio.run(self.service.poll(self.config, {}))

        self.assertEqual(notifications, [])
        self.assertEqual(state["uids"]["100"]["last_dynamic_id"], "dyn-3")
        self.assertEqual(state["uids"]["100"]["last_video_id"], "BV3")
        self.assertFalse(state["uids"]["100"]["last_live_active"])

    def test_second_poll_sends_all_unseen_dynamic_and_video_updates(self) -> None:
        initial_state, _ = asyncio.run(self.service.poll(self.config, {}))

        self.gateway.dynamic_posts["100"].insert(
            0,
            BilibiliDynamicPost(
                id="dyn-4",
                text="第四条动态",
                url="https://t.bilibili.com/dyn-4",
                rich_nodes=[BilibiliRichTextNode(kind="text", text="第四条动态")],
                comment_oid=3004,
                comment_type=17,
            ),
        )
        self.gateway.video_posts["100"].insert(
            0,
            BilibiliVideoPost(id="BV4", title="第四个视频", url="https://www.bilibili.com/video/BV4", comment_oid=2004),
        )

        updated_state, notifications = asyncio.run(self.service.poll(self.config, initial_state))

        self.assertEqual([item.kind for item in notifications], ["dynamic", "video"])
        self.assertEqual(updated_state["uids"]["100"]["last_dynamic_id"], "dyn-4")
        self.assertEqual(updated_state["uids"]["100"]["last_video_id"], "BV4")

    def test_live_notification_only_on_transition_to_live(self) -> None:
        initial_state, _ = asyncio.run(self.service.poll(self.config, {}))

        self.gateway.live_status["100"] = BilibiliLiveStatus(
            is_live=True,
            title="今晚直播",
            room_id="123",
            url="https://live.bilibili.com/123",
        )
        updated_state, notifications = asyncio.run(self.service.poll(self.config, initial_state))

        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].kind, "live")
        self.assertTrue(updated_state["uids"]["100"]["last_live_active"])

        repeated_state, repeated_notifications = asyncio.run(self.service.poll(self.config, updated_state))
        self.assertEqual(repeated_notifications, [])
        self.assertTrue(repeated_state["uids"]["100"]["last_live_active"])

    def test_comment_notification_only_for_new_target_comments(self) -> None:
        initial_state, notifications = asyncio.run(self.service.poll(self.config, {}))

        self.assertEqual(notifications, [])

        self.gateway.comments["video:2003"].insert(
            0,
            BilibiliCommentPost(
                id="9002",
                author_uid="100",
                author_name="测试账号",
                text="这是新评论",
                created_at=200,
                is_reply=True,
            ),
        )

        updated_state, updated_notifications = asyncio.run(self.service.poll(self.config, initial_state))

        self.assertEqual(len(updated_notifications), 1)
        self.assertEqual(updated_notifications[0].kind, "comment")
        self.assertIn("回复了评论", updated_notifications[0].title)
        self.assertEqual(updated_state["uids"]["100"]["comment_resources"]["video:2003"]["last_comment_id"], "9002")
