import asyncio
import json
import unittest

from asoul_bilibili import (
    BilibiliCommentPost,
    BilibiliCommentResource,
    BilibiliDynamicPost,
    BilibiliGateway,
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


class FakeUserForLiveInfo:
    def __init__(self, payload) -> None:
        self.payload = payload

    async def get_live_info(self):
        return self.payload


class ParsingGateway(BilibiliGateway):
    def __init__(self) -> None:
        super().__init__(request_client="aiohttp", credential_data={})
        self.live_info_payload = {}

    def _new_user(self, uid: str):
        return FakeUserForLiveInfo(self.live_info_payload)


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


class BilibiliParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.gateway = ParsingGateway()

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
        self.assertEqual(post.url, "https://live.bilibili.com/blackboard/reserve")

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
