import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from asoul_core import AVATAR_NAMES, PLUGIN_DIR, ScheduleItem

logger = logging.getLogger(__name__)


class ScheduleImageRenderer:
    async def render_schedule_image(
        self,
        items: List[ScheduleItem],
        target_day,
        title_text: str,
    ) -> str:
        try:
            return await asyncio.to_thread(self._render_schedule_image_local, items, target_day, title_text)
        except Exception:
            logger.exception("本地 Pillow 渲染失败")
            raise

    def _render_schedule_image_local(
        self,
        items: List[ScheduleItem],
        target_day,
        title_text: str,
    ) -> str:
        from PIL import Image, ImageDraw

        width = 1080
        outer_padding = 28
        panel_width = width - outer_padding * 2
        header_height = 216
        footer_height = 48
        list_gap = 18
        row_gap = 16

        work_dir = Path(tempfile.mkdtemp(prefix="asoul_schedule_", dir="/tmp"))
        output_path = work_dir / "today_schedule.png"

        font_title = self._load_pillow_font(54)
        font_subtitle = self._load_pillow_font(22)
        font_count = self._load_pillow_font(28)
        font_time = self._load_pillow_font(34)
        font_label = self._load_pillow_font(18)
        font_hosts = self._load_pillow_font(24)
        font_content = self._load_pillow_font(32)
        font_empty = self._load_pillow_font(30)
        font_footer = self._load_pillow_font(14)

        measure_image = Image.new("RGBA", (width, 10), (0, 0, 0, 0))
        measure_draw = ImageDraw.Draw(measure_image)
        wrapped_items: List[Tuple[ScheduleItem, List[str], int]] = []
        avatar_slot_width = 214
        content_width = panel_width - 250 - avatar_slot_width - 36
        total_rows_height = 0

        for item in items:
            content_lines = self._wrap_text_lines(
                measure_draw,
                item.content,
                font_content,
                content_width,
                max_lines=2,
            )
            content_height = self._measure_lines_height(measure_draw, content_lines, font_content, 10)
            row_height = max(136, 82 + content_height)
            wrapped_items.append((item, content_lines, row_height))
            total_rows_height += row_height

        if wrapped_items:
            list_height = total_rows_height + row_gap * (len(wrapped_items) - 1) + list_gap * 2
        else:
            list_height = 160

        height = outer_padding * 2 + header_height + list_height + footer_height
        image = Image.new("RGBA", (width, height), "#f3ebdf")
        draw = ImageDraw.Draw(image)

        draw.ellipse((-120, -80, 420, 300), fill="#efd4c2")
        draw.ellipse((760, -40, 1160, 280), fill="#dce8df")
        draw.rounded_rectangle(
            (
                outer_padding,
                outer_padding,
                width - outer_padding,
                height - outer_padding,
            ),
            radius=32,
            fill=(255, 250, 244, 242),
            outline=(255, 255, 255, 180),
            width=2,
        )
        draw.rounded_rectangle(
            (
                outer_padding,
                outer_padding,
                width - outer_padding,
                outer_padding + header_height,
            ),
            radius=32,
            fill="#eee0cf",
        )
        draw.line(
            (
                outer_padding + 28,
                outer_padding + header_height,
                width - outer_padding - 28,
                outer_padding + header_height,
            ),
            fill="#d8cabb",
            width=2,
        )

        panel_left = outer_padding + 40
        title_top = outer_padding + 40
        draw.rounded_rectangle(
            (panel_left, title_top, panel_left + 146, title_top + 38),
            radius=18,
            fill="#f4d8c8",
        )
        draw.text((panel_left + 18, title_top + 8), "A-SOUL LIVE", font=font_label, fill="#c56d49")
        draw.text(
            (panel_left, title_top + 56),
            f"{target_day.strftime('%Y-%m-%d')} {title_text}",
            font=font_title,
            fill="#201a17",
        )
        draw.text(
            (panel_left, title_top + 122),
            "今日排班",
            font=font_subtitle,
            fill="#74685f",
        )
        count_text = f"{len(items)} 条安排"
        count_box = draw.textbbox((0, 0), count_text, font=font_count)
        count_width = count_box[2] - count_box[0]
        count_x = width - outer_padding - 44 - count_width
        count_y = outer_padding + header_height - 48
        draw.text((count_x, count_y), count_text, font=font_count, fill="#c56d49")

        list_top = outer_padding + header_height + list_gap
        list_left = outer_padding + 28
        row_y = list_top

        if wrapped_items:
            for item, content_lines, row_height in wrapped_items:
                row_bottom = row_y + row_height
                draw.rounded_rectangle(
                    (list_left, row_y, width - outer_padding - 28, row_bottom),
                    radius=26,
                    fill="#fffaf4",
                    outline="#eadbc9",
                    width=2,
                )
                draw.rounded_rectangle(
                    (list_left + 24, row_y + 22, list_left + 168, row_y + row_height - 22),
                    radius=24,
                    fill="#f1e4d3",
                )

                time_box = draw.textbbox((0, 0), item.start_text, font=font_time)
                time_width = time_box[2] - time_box[0]
                time_height = time_box[3] - time_box[1]
                time_x = list_left + 96 - time_width / 2
                time_y = row_y + row_height / 2 - time_height / 2 - 4
                draw.text((time_x, time_y), item.start_text, font=font_time, fill="#201a17")

                text_left = list_left + 204
                avatar_left = width - outer_padding - 28 - avatar_slot_width
                label_width = self._text_width(draw, item.label, font_label) + 26
                draw.rounded_rectangle(
                    (text_left, row_y + 22, text_left + label_width, row_y + 52),
                    radius=15,
                    fill="#201a17",
                )
                draw.text((text_left + 13, row_y + 28), item.label, font=font_label, fill="#fff7ef")
                draw.text((text_left, row_y + 64), item.hosts_text, font=font_hosts, fill="#74685f")
                self._draw_multiline_text(
                    draw,
                    (text_left, row_y + 96),
                    content_lines,
                    font_content,
                    "#201a17",
                    line_spacing=10,
                )
                self._paste_item_avatars(
                    image=image,
                    hosts=item.hosts,
                    left=avatar_left + 12,
                    top=row_y + 18,
                    slot_width=avatar_slot_width - 24,
                    slot_height=row_height - 36,
                )
                row_y = row_bottom + row_gap
        else:
            empty_text = "今天还没有查到直播安排"
            empty_box = draw.textbbox((0, 0), empty_text, font=font_empty)
            empty_width = empty_box[2] - empty_box[0]
            draw.text(
                ((width - empty_width) / 2, list_top + 40),
                empty_text,
                font=font_empty,
                fill="#74685f",
            )

        footer_text = "AstrBot Plugin · A-SOUL Calendar"
        footer_box = draw.textbbox((0, 0), footer_text, font=font_footer)
        footer_width = footer_box[2] - footer_box[0]
        draw.text(
            (width - outer_padding - 28 - footer_width, height - outer_padding - 26),
            footer_text,
            font=font_footer,
            fill="#8c8178",
        )

        image.save(output_path, format="PNG")
        return str(output_path)

    def _find_font_file(self) -> Optional[str]:
        candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
            "/usr/share/fonts/opentype/noto/NotoSerifCJKsc-Regular.otf",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/opentype/source-han-sans/SourceHanSansCN-Regular.otf",
            "/usr/share/fonts/opentype/sourcehansans/SourceHanSansCN-Regular.otf",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansSC-Regular.otf",
            "/usr/share/fonts/truetype/arphic/ukai.ttc",
            str(PLUGIN_DIR / "font.ttf"),
            str(PLUGIN_DIR / "font.otf"),
            str(PLUGIN_DIR / "GenJyuuGothic-Normal-2.ttf"),
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ]
        for candidate in candidates:
            if Path(candidate).exists():
                return candidate

        fc_match = shutil.which("fc-match")
        if fc_match:
            try:
                result = subprocess.run(
                    [fc_match, "-f", "%{file}\n", "sans:lang=zh-cn"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                font_path = result.stdout.strip().splitlines()[0]
                if font_path and Path(font_path).exists():
                    return font_path
            except Exception:
                pass

        return None

    def _load_pillow_font(self, size: int):
        from PIL import ImageFont

        font_file = self._find_font_file()
        if font_file:
            try:
                return ImageFont.truetype(font_file, size=size)
            except Exception:
                logger.warning("字体加载失败: %s", font_file)
        return ImageFont.load_default()

    def _wrap_text_lines(self, draw, text: str, font, max_width: int, max_lines: int = 2) -> List[str]:
        compact = " ".join(text.split())
        if not compact:
            return [""]

        lines: List[str] = []
        current = ""
        for char in compact:
            trial = current + char
            if self._text_width(draw, trial, font) <= max_width:
                current = trial
                continue

            if current:
                lines.append(current)
            current = char
            if len(lines) >= max_lines - 1:
                break

        remainder = compact[len("".join(lines)):]
        if remainder:
            tail = ""
            for char in remainder:
                trial = tail + char
                suffix = "…" if len(remainder) < len(compact) or len(lines) >= max_lines - 1 else ""
                if self._text_width(draw, trial + suffix, font) <= max_width:
                    tail = trial
                else:
                    break
            if len(lines) >= max_lines - 1 and len(remainder) > len(tail):
                tail = tail.rstrip() + "…"
            lines.append(tail or remainder[:1])
        elif current:
            lines.append(current)

        return lines[:max_lines]

    def _measure_lines_height(self, draw, lines: List[str], font, line_spacing: int) -> int:
        if not lines:
            return 0
        bbox = draw.textbbox((0, 0), "测", font=font)
        line_height = bbox[3] - bbox[1]
        return line_height * len(lines) + line_spacing * (len(lines) - 1)

    def _text_width(self, draw, text: str, font) -> int:
        if not text:
            return 0
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]

    def _draw_multiline_text(self, draw, pos, lines: List[str], font, fill: str, line_spacing: int) -> None:
        x, y = pos
        bbox = draw.textbbox((0, 0), "测", font=font)
        line_height = bbox[3] - bbox[1]
        for index, line in enumerate(lines):
            draw.text((x, y + index * (line_height + line_spacing)), line, font=font, fill=fill)

    def _paste_item_avatars(
        self,
        image,
        hosts: List[str],
        left: int,
        top: int,
        slot_width: int,
        slot_height: int,
    ) -> None:
        from PIL import Image

        avatar_map = self._get_avatar_path_map()
        avatar_paths = [avatar_map[host] for host in hosts if host in avatar_map]
        if not avatar_paths:
            return

        resampling = getattr(Image, "Resampling", Image)
        count = len(avatar_paths)
        if count == 1:
            gap = 0
            avatar_size = min(112, slot_height, slot_width)
        elif count == 2:
            gap = 8
            avatar_size = min(84, slot_height, (slot_width - gap) // 2)
        else:
            gap = 6
            avatar_size = min(62, slot_height, max(36, (slot_width - gap * (count - 1)) // count))

        total_width = count * avatar_size + (count - 1) * gap
        start_x = left + max(0, (slot_width - total_width) // 2)
        base_y = top + max(0, (slot_height - avatar_size) // 2)
        for index, avatar_path in enumerate(avatar_paths):
            avatar = Image.open(avatar_path).convert("RGBA")
            avatar.thumbnail((avatar_size, avatar_size), resampling.LANCZOS)
            x = start_x + index * (avatar_size + gap)
            y = base_y + max(0, avatar_size - avatar.height) // 2
            image.alpha_composite(avatar, (x, y))

    def _get_avatar_path_map(self) -> Dict[str, Path]:
        avatar_map: Dict[str, Path] = {}
        for name in AVATAR_NAMES:
            path = PLUGIN_DIR / f"{name}.png"
            if path.exists():
                avatar_map[name] = path
        return avatar_map
