import asyncio
import unittest
from unittest.mock import patch

from test_asoul_push_targets import _install_astrbot_stubs, _load_main_module

_install_astrbot_stubs()

from asoul_bilibili import (
    KV_BILIBILI_PROFILE_CACHE,
    BilibiliAuthorCardProfile,
)


NOW_TS = 1_700_000_000


class BilibiliProfileCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        main = _load_main_module()
        self.plugin = main.ASoulPlugin(
            main.Context(),
            config={"enabled": False, "target_uids": ["100"]},
        )
        self.runtime = self.plugin._bilibili_runtime

    def test_missing_profile_is_fetched_and_persisted(self) -> None:
        fresh = BilibiliAuthorCardProfile(
            uid="100",
            name="新昵称",
            follower=726000,
            fetched_at=NOW_TS,
        )

        async def fetch(_uid: str) -> BilibiliAuthorCardProfile:
            return fresh

        self.runtime.gateway.get_user_card_profile = fetch
        with patch("asoul_bilibili_runtime.time.time", return_value=NOW_TS):
            result = asyncio.run(self.runtime.get_author_card_profile("100"))

        self.assertEqual(result, fresh)
        persisted = self.plugin._kv_store[KV_BILIBILI_PROFILE_CACHE]
        self.assertEqual(persisted["100"]["name"], "新昵称")
        self.assertEqual(persisted["100"]["follower"], 726000)

    def test_profile_cache_is_restored_from_independent_kv(self) -> None:
        self.plugin._kv_store[KV_BILIBILI_PROFILE_CACHE] = {
            "100": {
                "uid": "100",
                "name": "缓存昵称",
                "avatar_url": "https://i.example/avatar.png",
                "follower": 12,
                "fetched_at": NOW_TS,
            }
        }

        asyncio.run(self.runtime.load_state())

        profile = self.runtime.profile_cache["100"]
        self.assertEqual(profile.name, "缓存昵称")
        self.assertEqual(profile.follower, 12)

    def test_stale_profile_returns_immediately_and_refreshes_in_background(self) -> None:
        stale = BilibiliAuthorCardProfile(
            uid="100",
            name="旧昵称",
            total_likes=5,
            following=7,
            follower=1,
            fetched_at=NOW_TS - 21601,
        )
        refreshed = BilibiliAuthorCardProfile(
            uid="100",
            name="100",
            follower=2,
            fetched_at=NOW_TS,
        )
        self.runtime.profile_cache = {"100": stale}
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()

        async def fetch(_uid: str) -> BilibiliAuthorCardProfile:
            refresh_started.set()
            await release_refresh.wait()
            return refreshed

        self.runtime.gateway.get_user_card_profile = fetch

        async def exercise() -> BilibiliAuthorCardProfile:
            result = await self.runtime.get_author_card_profile("100")
            await asyncio.wait_for(refresh_started.wait(), timeout=0.05)
            self.assertEqual(result, stale)
            release_refresh.set()
            await asyncio.gather(*self.runtime._profile_refresh_tasks.values())
            return result

        with patch("asoul_bilibili_runtime.time.time", return_value=NOW_TS):
            asyncio.run(exercise())

        merged = self.runtime.profile_cache["100"]
        self.assertEqual(merged.name, "旧昵称")
        self.assertEqual(merged.total_likes, 5)
        self.assertEqual(merged.following, 7)
        self.assertEqual(merged.follower, 2)
        self.assertEqual(merged.fetched_at, NOW_TS)

    def test_failed_background_refresh_keeps_stale_profile(self) -> None:
        stale = BilibiliAuthorCardProfile(
            uid="100",
            name="可用旧资料",
            fetched_at=NOW_TS - 21601,
        )
        self.runtime.profile_cache = {"100": stale}

        async def fail(_uid: str) -> BilibiliAuthorCardProfile:
            raise RuntimeError("profile unavailable")

        self.runtime.gateway.get_user_card_profile = fail

        async def exercise() -> BilibiliAuthorCardProfile:
            result = await self.runtime.get_author_card_profile("100")
            await asyncio.gather(
                *self.runtime._profile_refresh_tasks.values(),
                return_exceptions=True,
            )
            return result

        with patch("asoul_bilibili_runtime.time.time", return_value=NOW_TS):
            result = asyncio.run(exercise())

        self.assertEqual(result, stale)
        self.assertEqual(self.runtime.profile_cache["100"], stale)

    def test_uncached_profile_fetch_obeys_total_budget(self) -> None:
        async def wait_forever(_uid: str) -> BilibiliAuthorCardProfile:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        self.runtime.gateway.get_user_card_profile = wait_forever
        seed = BilibiliAuthorCardProfile(uid="100", name="动态昵称")

        with patch("asoul_bilibili_runtime.PROFILE_FETCH_TIMEOUT_SECONDS", 0.01):
            result = asyncio.run(
                self.runtime.get_author_card_profile("100", fallback=seed)
            )

        self.assertEqual(result, seed)
        self.assertNotIn("100", self.runtime.profile_cache)

    def test_partial_uncached_profile_keeps_dynamic_author_identity(self) -> None:
        partial = BilibiliAuthorCardProfile(
            uid="100",
            name="100",
            follower=2,
            fetched_at=NOW_TS,
        )

        async def fetch(_uid: str) -> BilibiliAuthorCardProfile:
            return partial

        self.runtime.gateway.get_user_card_profile = fetch
        fallback = BilibiliAuthorCardProfile(
            uid="100",
            name="动态昵称",
            avatar_url="https://i.example/dynamic-avatar.png",
        )

        result = asyncio.run(
            self.runtime.get_author_card_profile("100", fallback=fallback)
        )

        self.assertEqual(result.name, "动态昵称")
        self.assertEqual(result.avatar_url, fallback.avatar_url)
        self.assertEqual(result.follower, 2)


if __name__ == "__main__":
    unittest.main()
