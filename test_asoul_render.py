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


if __name__ == "__main__":
    unittest.main()
