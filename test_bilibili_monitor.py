import asyncio
import json
import unittest
from unittest.mock import patch

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
    build_bilibili_push_config,
    normalize_bilibili_uid,
)

NOW_TS = 1_700_000_000


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

    async def get_latest_dynamics(self, uid: str, limit: int):
        return self.dynamic_posts.get(uid, [])[:limit]

    async def get_latest_videos(self, uid: str, limit: int):
        return self.video_posts.get(uid, [])[:limit]

    async def get_live_status(self, uid: str):
        return self.live_status.get(uid)

    async def get_live_status_by_uid(self, uid: str):
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

    def test_first_poll_only_initializes_state(self) -> None:
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            state, notifications = asyncio.run(self.service.poll(self.config, {}))

        self.assertEqual(notifications, [])
        self.assertEqual(state["uids"]["100"]["last_dynamic_id"], "dyn-3")
        self.assertFalse(state["uids"]["100"]["last_live_active"])

    def test_second_poll_sends_all_unseen_dynamic_and_video_updates(self) -> None:
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            initial_state, _ = asyncio.run(self.service.poll(self.config, {}))

        self.gateway.dynamic_posts["100"] = [
            BilibiliDynamicPost(
                id="dyn-video-4",
                text="投稿了新视频",
                url="https://www.bilibili.com/video/BV4",
                title="第四个视频",
                cover_url="https://i0.hdslb.com/bfs/archive/video-cover-4.jpg",
                image_urls=["https://i0.hdslb.com/bfs/archive/video-cover-4.jpg"],
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
            updated_state, notifications = asyncio.run(self.service.poll(self.config, initial_state))

        self.assertEqual([item.kind for item in notifications], ["dynamic", "video"])
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
            updated_state, notifications = asyncio.run(self.service.poll(self.config, stale_state))

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
            updated_state, notifications = asyncio.run(self.service.poll(self.config, stale_state))

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
                self.service.poll(self.config, persisted_state)
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
            updated_state, notifications = asyncio.run(self.service.poll(self.config, stale_state))

        self.assertEqual(notifications, [])
        self.assertEqual(updated_state["uids"]["100"]["last_dynamic_id"], "dyn-video-4")

    def test_live_notification_only_on_transition_to_live(self) -> None:
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            initial_state, _ = asyncio.run(self.service.poll(self.config, {}))

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
            updated_state, notifications = asyncio.run(self.service.poll(self.config, initial_state))

        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0].kind, "live")
        self.assertTrue(updated_state["uids"]["100"]["last_live_active"])

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            repeated_state, repeated_notifications = asyncio.run(self.service.poll(self.config, updated_state))
        self.assertEqual(repeated_notifications, [])
        self.assertTrue(repeated_state["uids"]["100"]["last_live_active"])

    def test_comment_notification_only_for_new_target_comments(self) -> None:
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
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

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            updated_state, updated_notifications = asyncio.run(self.service.poll(self.config, initial_state))

        self.assertEqual(len(updated_notifications), 1)
        self.assertEqual(updated_notifications[0].kind, "comment")
        self.assertIn("回复了评论", updated_notifications[0].title)
        self.assertEqual(updated_state["uids"]["100"]["comment_resources"]["video:2003"]["last_comment_id"], "9002")

    def test_second_poll_delivers_two_reserve_dynamics_in_order(self) -> None:
        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            initial_state, _ = asyncio.run(self.service.poll(self.config, {}))

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
            updated_state, notifications = asyncio.run(self.service.poll(self.config, initial_state))

        self.assertEqual(
            [notification.url for notification in notifications if notification.kind == "dynamic"],
            [
                "https://live.bilibili.com/blackboard/reserve-1",
                "https://live.bilibili.com/blackboard/reserve-2",
            ],
        )
        self.assertEqual(updated_state["uids"]["100"]["last_dynamic_id"], "dyn-reserve-2")


class BilibiliConfigParsingTest(unittest.TestCase):
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
