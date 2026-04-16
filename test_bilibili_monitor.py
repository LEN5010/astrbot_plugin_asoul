import asyncio
import json
import types
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

    async def get_recent_comments(self, resource: BilibiliCommentResource, stop_comment_ids=None, max_pages=None):
        self.comment_fetch_requests.append(
            {
                "key": resource.key,
                "stop_comment_ids": list(stop_comment_ids or []),
                "max_pages": max_pages,
            }
        )
        return list(self.comments.get(resource.key, []))


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
        super().__init__(request_client="aiohttp", credential_data={})
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

    async def get_comments_lazy(self, oid, type_, offset="", order=None, credential=None):
        self.calls.append(str(offset or ""))
        return self.pages.get(str(offset or ""), {})


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
        self.assertEqual(updated_notifications[0].comment_action_text, "回复了评论")
        self.assertEqual(updated_notifications[0].comment_resource_kind, "视频")
        self.assertEqual(updated_notifications[0].comment_resource_title, "第三个视频")
        self.assertEqual(updated_state["uids"]["100"]["comment_resources"]["video:2003"]["last_comment_id"], "9002")

    def test_fetch_uid_snapshot_passes_known_comment_ids_to_comment_fetcher(self) -> None:
        previous_state = {
            "author_name": "测试账号",
            "comment_resources": {
                "video:2003": {
                    "initialized": True,
                    "last_comment_id": "9002",
                    "recent_comment_ids": ["9002", "9001"],
                }
            },
        }

        asyncio.run(
            self.service.fetch_uid_snapshot(
                self.config,
                "100",
                previous_state=previous_state,
            )
        )

        requests_by_key = {
            item["key"]: item for item in self.gateway.comment_fetch_requests
        }
        self.assertEqual(
            requests_by_key["video:2003"]["stop_comment_ids"],
            ["9002", "9001"],
        )
        self.assertEqual(requests_by_key["video:2003"]["max_pages"], 5)

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

    def test_pinned_latest_dynamic_still_blocks_older_dynamic_replay(self) -> None:
        self.gateway.dynamic_page_payload = {
            "items": [
                {
                    "id_str": "dyn-5",
                    "basic": {
                        "comment_id_str": "3005",
                        "comment_type": 17,
                    },
                    "modules": {
                        "module_tag": {"text": "置顶"},
                        "module_author": {
                            "pub_ts": NOW_TS - 60,
                        },
                        "module_dynamic": {
                            "desc": {"text": "最新动态但被置顶"},
                        },
                    },
                },
                {
                    "id_str": "dyn-4",
                    "basic": {
                        "comment_id_str": "3004",
                        "comment_type": 17,
                    },
                    "modules": {
                        "module_author": {
                            "pub_ts": NOW_TS - 120,
                        },
                        "module_dynamic": {
                            "desc": {"text": "旧的漏发动态"},
                        },
                    },
                },
            ]
        }

        posts, stop_found = asyncio.run(
            self.gateway.get_recent_dynamics_with_status(
                "100",
                stop_at_id="dyn-5",
            )
        )

        self.assertEqual(posts, [])
        self.assertTrue(stop_found)

    def test_recent_pinned_dynamic_is_still_collected_for_delivery(self) -> None:
        self.gateway.dynamic_page_payload = {
            "items": [
                {
                    "id_str": "dyn-6",
                    "basic": {
                        "comment_id_str": "3006",
                        "comment_type": 17,
                    },
                    "modules": {
                        "module_tag": {"text": "置顶"},
                        "module_author": {
                            "pub_ts": NOW_TS - 60,
                        },
                        "module_dynamic": {
                            "desc": {"text": "刚发出就被置顶的新动态"},
                        },
                    },
                }
            ]
        }

        with patch("asoul_bilibili.time.time", return_value=NOW_TS):
            posts, stop_found = asyncio.run(
                self.gateway.get_recent_dynamics_with_status(
                    "100",
                    stop_at_id=None,
                )
            )

        self.assertTrue(stop_found)
        self.assertEqual([post.id for post in posts], ["dyn-6"])

    def test_old_pinned_dynamic_is_not_replayed_as_new_update(self) -> None:
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
        self.assertEqual(posts, [])

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

        self.assertEqual([comment.id for comment in comments], ["9004", "9003"])
        self.assertEqual(self.gateway.comment_module.calls, ["", "page-2"])

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
