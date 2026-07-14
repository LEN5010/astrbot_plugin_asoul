import asyncio
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from asoul_bilibili import (
    BilibiliAdditionalCard,
    BilibiliAuthorCardProfile,
    BilibiliEngagementStats,
    BilibiliNotification,
    BilibiliRichTextNode,
)
from asoul_bilibili_card import (
    BilibiliCardRenderer,
    build_card_context,
    format_card_number,
    render_rich_text_html,
)


class BilibiliCardFormattingTest(unittest.TestCase):
    def test_card_context_uses_aituo_brand(self) -> None:
        notification = BilibiliNotification(
            kind="dynamic",
            uid="100",
            author_name="测试账号",
            title="",
            url="https://t.bilibili.com/1",
        )

        with patch(
            "asoul_bilibili_card.build_brand_logo_data_uri",
            return_value="data:image/jpeg;base64,logo",
        ):
            with patch(
                "asoul_bilibili_card.build_qr_data_uri",
                return_value="data:image/png;base64,qr",
            ):
                context = build_card_context(notification)

        self.assertEqual(context["brand_name"], "爱驼推送")
        self.assertEqual(
            context["brand_logo_data_uri"],
            "data:image/jpeg;base64,logo",
        )

    def test_template_keeps_all_data_sections_with_simple_aituo_brand(self) -> None:
        template = (
            Path(__file__).resolve().parent / "templates" / "bilibili_card.html"
        ).read_text(encoding="utf-8")

        self.assertIn("{{ brand_name }}", template)
        self.assertIn("{{ brand_logo_data_uri", template)
        self.assertIn("本条内容", template)
        self.assertIn("UP资料", template)
        self.assertIn("{{ stats_note }}", template)
        self.assertNotIn("ASTRBOT · BILIBILI CARD", template)

    def test_format_card_number_uses_chinese_units_and_missing_marker(self) -> None:
        self.assertEqual(format_card_number(None), "--")
        self.assertEqual(format_card_number(9999), "9999")
        self.assertEqual(format_card_number(18807000), "1880.7万")
        self.assertEqual(format_card_number(100000000), "1亿")

    def test_rich_text_escapes_user_html_and_only_emits_safe_generated_tags(self) -> None:
        rendered = render_rich_text_html(
            [
                BilibiliRichTextNode(kind="text", text='<script>alert("x")</script>\n'),
                BilibiliRichTextNode(
                    kind="emoji",
                    text="[星星眼]",
                    image_url="https://i.example/emoji.png",
                ),
                BilibiliRichTextNode(
                    kind="link",
                    text="#话题#",
                    url="https://search.bilibili.com/all?keyword=topic",
                ),
                BilibiliRichTextNode(
                    kind="link",
                    text="危险链接",
                    url="javascript:alert(1)",
                ),
            ],
            "",
        )

        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("<br>", rendered)
        self.assertIn('class="inline-emoji"', rendered)
        self.assertIn('href="https://search.bilibili.com/all?keyword=topic"', rendered)
        self.assertNotIn("javascript:", rendered)
        self.assertIn("危险链接", rendered)

    def test_card_context_contains_profile_stats_additional_and_nine_images(self) -> None:
        notification = BilibiliNotification(
            kind="dynamic",
            uid="100",
            author_name="测试账号",
            title="",
            text="正文",
            url="https://t.bilibili.com/123",
            image_urls=[f"https://i.example/{index}.png" for index in range(12)],
            published_at=1_700_000_000,
            author_profile=BilibiliAuthorCardProfile(
                uid="100",
                name="测试账号",
                avatar_url="https://i.example/avatar.png",
                total_likes=18807000,
                following=23,
                follower=726000,
            ),
            stats=BilibiliEngagementStats(77, 6, 2),
            additional_card=BilibiliAdditionalCard(
                kind="reserve",
                title="预约直播",
                subtitle="07-16 12:00",
                badge="预约",
                url="https://live.bilibili.com/1",
            ),
        )

        with patch(
            "asoul_bilibili_card.build_qr_data_uri",
            return_value="data:image/png;base64,qr",
        ):
            context = build_card_context(notification, generated_at=1_700_000_100)

        self.assertEqual(context["author"]["likes"], "1880.7万")
        self.assertEqual(context["stats"]["like"], "77")
        self.assertEqual(context["additional"]["kind"], "reserve")
        self.assertEqual(len(context["images"]), 9)
        self.assertEqual(context["qr_data_uri"], "data:image/png;base64,qr")

    def test_fallback_video_stats_add_refresh_note_to_context(self) -> None:
        notification = BilibiliNotification(
            kind="video",
            uid="100",
            author_name="测试账号",
            title="新视频",
            url="https://www.bilibili.com/video/BV1",
            stats=BilibiliEngagementStats(1, 2, 3),
            stats_are_fallback=True,
        )

        with patch(
            "asoul_bilibili_card.build_qr_data_uri",
            return_value="data:image/png;base64,qr",
        ):
            context = build_card_context(notification)

        self.assertEqual(context["stats_note"], "数据暂未刷新")


class BilibiliCardRendererTest(unittest.TestCase):
    def test_same_notification_renders_once_and_cleanup_removes_created_card(self) -> None:
        class FakeOwner:
            def __init__(self, output_path: str) -> None:
                self.output_path = output_path
                self.calls = []

            async def html_render(
                self,
                template,
                data,
                *,
                return_url,
                options,
            ):
                self.calls.append(
                    {
                        "template": template,
                        "data": data,
                        "return_url": return_url,
                        "options": options,
                    }
                )
                Path(self.output_path).write_bytes(b"x" * 2048)
                return self.output_path

        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = str(Path(temp_dir) / "card.png")
            owner = FakeOwner(output_path)
            renderer = BilibiliCardRenderer(owner)
            notification = BilibiliNotification(
                kind="video",
                uid="100",
                author_name="测试账号",
                title="新视频",
                url="https://www.bilibili.com/video/BV1",
                content_id="BV1",
            )

            async def exercise() -> tuple[str, str]:
                first = await renderer.render(notification)
                second = await renderer.render(notification)
                return first, second

            with patch(
                "asoul_bilibili_card.build_qr_data_uri",
                return_value="data:image/png;base64,qr",
            ):
                first, second = asyncio.run(exercise())

            self.assertEqual(first, output_path)
            self.assertEqual(second, output_path)
            self.assertEqual(len(owner.calls), 1)
            self.assertFalse(owner.calls[0]["return_url"])
            self.assertTrue(owner.calls[0]["options"]["full_page"])

            asyncio.run(renderer.cleanup())
            self.assertFalse(Path(output_path).exists())

    def test_stats_change_invalidates_render_cache(self) -> None:
        class FakeOwner:
            def __init__(self, output_path: str) -> None:
                self.output_path = output_path
                self.calls = []

            async def html_render(self, template, data, *, return_url, options):
                self.calls.append(data["stats"])
                Path(self.output_path).write_bytes(b"x" * 2048)
                return self.output_path

        with tempfile.TemporaryDirectory() as temp_dir:
            owner = FakeOwner(str(Path(temp_dir) / "card.png"))
            renderer = BilibiliCardRenderer(owner)
            notification = BilibiliNotification(
                kind="video",
                uid="100",
                author_name="测试账号",
                title="新视频",
                url="https://www.bilibili.com/video/BV1",
                content_id="dyn-video",
                stats=BilibiliEngagementStats(1, 2, 3),
            )

            async def exercise():
                await renderer.render(notification)
                await renderer.render(
                    replace(
                        notification,
                        stats=BilibiliEngagementStats(4, 5, 6),
                    )
                )

            with patch(
                "asoul_bilibili_card.build_qr_data_uri",
                return_value="data:image/png;base64,qr",
            ):
                asyncio.run(exercise())

            self.assertEqual(len(owner.calls), 2)


if __name__ == "__main__":
    unittest.main()
