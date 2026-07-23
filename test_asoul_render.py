import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import asoul_render
from asoul_render import ScheduleImageRenderer


class ScheduleImageRendererFontTest(unittest.TestCase):
    def test_find_font_file_prefers_bundled_font_before_system_fonts(self) -> None:
        plugin_dir = Path("/plugin/asoul")
        bundled_font = plugin_dir / "font.ttf"
        system_font = "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"

        def fake_exists(path: Path) -> bool:
            return str(path) in {str(bundled_font), system_font}

        with patch.object(asoul_render, "PLUGIN_DIR", plugin_dir), patch(
            "asoul_render.Path.exists",
            new=fake_exists,
        ):
            self.assertEqual(
                ScheduleImageRenderer()._find_font_file(),
                str(bundled_font),
            )

    def test_find_font_file_falls_back_to_system_font_when_bundled_fonts_missing(self) -> None:
        plugin_dir = Path("/plugin/asoul")
        system_font = "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf"

        def fake_exists(path: Path) -> bool:
            return str(path) == system_font

        with patch.object(asoul_render, "PLUGIN_DIR", plugin_dir), patch(
            "asoul_render.Path.exists",
            new=fake_exists,
        ), patch("asoul_render.shutil.which", return_value=None):
            self.assertEqual(
                ScheduleImageRenderer()._find_font_file(),
                system_font,
            )


class ScheduleImageRendererStickerTest(unittest.TestCase):
    def test_member_folder_randomly_selects_supported_sticker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_dir = Path(temp_dir)
            member_dir = plugin_dir / "贝拉"
            member_dir.mkdir()
            first = member_dir / "a.png"
            second = member_dir / "b.webp"
            ignored = member_dir / "note.txt"
            first.write_bytes(b"a")
            second.write_bytes(b"b")
            ignored.write_text("ignored", encoding="utf-8")

            with patch.object(asoul_render, "PLUGIN_DIR", plugin_dir), patch(
                "asoul_render.random.choice", side_effect=lambda items: items[-1]
            ):
                avatar_map = ScheduleImageRenderer()._get_avatar_path_map()

        self.assertEqual(avatar_map["贝拉"].name, "b.webp")

    def test_each_render_selection_can_choose_a_different_sticker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_dir = Path(temp_dir)
            member_dir = plugin_dir / "嘉然"
            member_dir.mkdir()
            (member_dir / "a.png").write_bytes(b"a")
            (member_dir / "b.png").write_bytes(b"b")
            selected_index = 0

            def choose(items):
                nonlocal selected_index
                selected = items[selected_index % len(items)]
                selected_index += 1
                return selected

            with patch.object(asoul_render, "PLUGIN_DIR", plugin_dir), patch(
                "asoul_render.random.choice", side_effect=choose
            ):
                renderer = ScheduleImageRenderer()
                first_map = renderer._get_avatar_path_map()
                second_map = renderer._get_avatar_path_map()

        self.assertNotEqual(first_map["嘉然"], second_map["嘉然"])

    def test_empty_member_folder_falls_back_to_legacy_single_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin_dir = Path(temp_dir)
            (plugin_dir / "心宜").mkdir()
            legacy = plugin_dir / "心宜.png"
            legacy.write_bytes(b"legacy")

            with patch.object(asoul_render, "PLUGIN_DIR", plugin_dir):
                avatar_map = ScheduleImageRenderer()._get_avatar_path_map()

        self.assertEqual(avatar_map["心宜"], legacy)


if __name__ == "__main__":
    unittest.main()
