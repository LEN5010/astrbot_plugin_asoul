import asyncio
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib import request
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

CALENDAR_URL = "https://asoul.love/calendar.ics"
CALENDAR_TTL = timedelta(minutes=10)
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")
PLUGIN_DIR = Path(__file__).resolve().parent
TRIGGER_TEXTS = {"直播数据", "今日直播"}
LIVE_KEYWORDS = {
    "直播",
    "开播",
    "live",
    "突击",
    "2d",
    "节目",
    "综艺",
    "线下",
    "歌会",
    "歌杂",
    "杂谈",
    "电台",
    "联动",
    "游戏",
    "演唱会",
    "birthday live",
    "sing",
}
NON_LIVE_KEYWORDS = {
    "投稿",
    "翻唱发布",
    "周边",
    "纪念",
    "生日",
    "周年",
    "首发",
}
MEMBER_ALIASES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("向晚", ("向晚", "ava")),
    ("贝拉", ("贝拉", "bella")),
    ("珈乐", ("珈乐", "carol")),
    ("嘉然", ("嘉然", "diana")),
    ("乃琳", ("乃琳", "eileen")),
    ("心宜", ("心宜", "fiona")),
    ("思诺", ("思诺", "gladys")),
    ("A-SOUL", ("a-soul", "asoul", "一个魂")),
)


@dataclass
class CalendarEvent:
    summary: str
    description: str
    location: str
    categories: str
    url: str
    status: str
    start: datetime
    end: datetime
    all_day: bool = False


@dataclass
class ScheduleItem:
    start: datetime
    start_text: str
    hosts: List[str]
    hosts_text: str
    content: str
    label: str


@register("astrbot_plugin_asoul", "LEN5010", "查询 A-SOUL 今日直播安排", "1.1.0")
class ASoulPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._calendar_cache: List[CalendarEvent] = []
        self._calendar_cache_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._calendar_lock = asyncio.Lock()

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_live_request(self, event: AstrMessageEvent):
        """用户发送“直播数据”时返回今日直播安排。"""
        if event.message_str.strip() not in TRIGGER_TEXTS:
            return

        event.stop_event()

        try:
            events = await self._get_today_live_events()
        except Exception:
            logger.exception("获取 A-SOUL 直播日历失败")
            yield event.plain_result("⚠️ 直播日历暂时不可用，请稍后再试。")
            return

        items = self._build_schedule_items(events)
        try:
            image_url = await self._render_schedule_image(items)
        except Exception:
            logger.exception("渲染直播图片失败")
            yield event.plain_result(self._format_schedule_fallback(items))
            return

        yield event.image_result(image_url)

    async def _get_today_live_events(self) -> List[CalendarEvent]:
        today = datetime.now(DISPLAY_TZ).date()
        calendar_events = await self._load_calendar_events()
        today_events = [
            event
            for event in calendar_events
            if self._is_same_day(event, today) and self._is_livestream_event(event)
        ]
        today_events.sort(key=lambda item: item.start)
        return today_events

    async def _load_calendar_events(self) -> List[CalendarEvent]:
        now = datetime.now(timezone.utc)
        if now < self._calendar_cache_expires_at and self._calendar_cache:
            return list(self._calendar_cache)

        async with self._calendar_lock:
            now = datetime.now(timezone.utc)
            if now < self._calendar_cache_expires_at and self._calendar_cache:
                return list(self._calendar_cache)

            try:
                text = await asyncio.to_thread(self._download_calendar_text)
                events = self._parse_calendar(text)
            except Exception:
                if self._calendar_cache:
                    logger.warning("直播日历刷新失败，继续使用缓存数据")
                    return list(self._calendar_cache)
                raise

            self._calendar_cache = events
            self._calendar_cache_expires_at = now + CALENDAR_TTL
            return list(events)

    def _download_calendar_text(self) -> str:
        req = request.Request(
            CALENDAR_URL,
            headers={"User-Agent": "astrbot-plugin-asoul/1.1.0"},
        )
        with request.urlopen(req, timeout=10) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")

    def _parse_calendar(self, text: str) -> List[CalendarEvent]:
        events: List[CalendarEvent] = []
        current_event: Optional[Dict[str, List[Tuple[Dict[str, str], str]]]] = None

        for line in self._unfold_ics_lines(text):
            if line == "BEGIN:VEVENT":
                current_event = {}
                continue
            if line == "END:VEVENT":
                if current_event:
                    event = self._build_event(current_event)
                    if event is not None:
                        events.append(event)
                current_event = None
                continue
            if current_event is None or ":" not in line:
                continue

            name, params, value = self._parse_content_line(line)
            current_event.setdefault(name, []).append((params, value))

        return events

    def _build_event(
        self,
        raw_event: Dict[str, List[Tuple[Dict[str, str], str]]],
    ) -> Optional[CalendarEvent]:
        dtstart = self._get_datetime(raw_event, "DTSTART")
        if dtstart is None:
            return None

        dtend = self._get_datetime(raw_event, "DTEND")
        start_items = raw_event.get("DTSTART", [])
        start_params = start_items[0][0] if start_items else {}
        all_day = start_params.get("VALUE", "").upper() == "DATE"

        summary = self._get_text(raw_event, "SUMMARY")
        description = self._get_text(raw_event, "DESCRIPTION")
        location = self._get_text(raw_event, "LOCATION")
        categories = self._get_text(raw_event, "CATEGORIES")
        url = self._get_text(raw_event, "URL")
        status = self._get_text(raw_event, "STATUS").upper()

        if dtend is None:
            duration = self._get_duration(raw_event)
            if duration is not None:
                dtend = dtstart + duration
            else:
                dtend = dtstart + (timedelta(days=1) if all_day else timedelta(hours=1))

        return CalendarEvent(
            summary=summary,
            description=description,
            location=location,
            categories=categories,
            url=url,
            status=status,
            start=dtstart,
            end=dtend,
            all_day=all_day,
        )

    def _get_text(
        self,
        raw_event: Dict[str, List[Tuple[Dict[str, str], str]]],
        key: str,
    ) -> str:
        items = raw_event.get(key)
        if not items:
            return ""
        return self._decode_ics_text(items[0][1]).strip()

    def _get_datetime(
        self,
        raw_event: Dict[str, List[Tuple[Dict[str, str], str]]],
        key: str,
    ) -> Optional[datetime]:
        items = raw_event.get(key)
        if not items:
            return None

        params, value = items[0]
        parsed_date = self._parse_ics_datetime(value, params)
        if isinstance(parsed_date, date) and not isinstance(parsed_date, datetime):
            return datetime.combine(parsed_date, time.min, DISPLAY_TZ)
        return parsed_date.astimezone(DISPLAY_TZ)

    def _get_duration(
        self,
        raw_event: Dict[str, List[Tuple[Dict[str, str], str]]],
    ) -> Optional[timedelta]:
        items = raw_event.get("DURATION")
        if not items:
            return None
        return self._parse_ics_duration(items[0][1])

    def _parse_content_line(self, line: str) -> Tuple[str, Dict[str, str], str]:
        key_part, value = line.split(":", 1)
        pieces = key_part.split(";")
        name = pieces[0].upper()
        params: Dict[str, str] = {}

        for piece in pieces[1:]:
            if "=" not in piece:
                continue
            param_key, param_value = piece.split("=", 1)
            params[param_key.upper()] = param_value.strip('"')

        return name, params, value

    def _unfold_ics_lines(self, text: str) -> List[str]:
        lines: List[str] = []
        for raw_line in text.splitlines():
            if raw_line.startswith((" ", "\t")) and lines:
                lines[-1] += raw_line[1:]
                continue
            lines.append(raw_line.rstrip("\r"))
        return lines

    def _parse_ics_datetime(self, value: str, params: Dict[str, str]) -> datetime | date:
        if params.get("VALUE", "").upper() == "DATE" or len(value) == 8:
            return datetime.strptime(value, "%Y%m%d").date()

        fmt = "%Y%m%dT%H%M%S" if len(value.rstrip("Z")) == 15 else "%Y%m%dT%H%M"
        if value.endswith("Z"):
            return datetime.strptime(value, fmt + "Z").replace(tzinfo=timezone.utc)

        tzid = params.get("TZID")
        tz = DISPLAY_TZ
        if tzid:
            try:
                tz = ZoneInfo(tzid)
            except Exception:
                logger.warning("未知日历时区 %s，回退到 Asia/Shanghai", tzid)

        return datetime.strptime(value, fmt).replace(tzinfo=tz)

    def _parse_ics_duration(self, value: str) -> Optional[timedelta]:
        match = re.fullmatch(
            r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
            value,
        )
        if not match:
            return None

        parts = {key: int(number or 0) for key, number in match.groupdict().items()}
        return timedelta(
            days=parts["days"],
            hours=parts["hours"],
            minutes=parts["minutes"],
            seconds=parts["seconds"],
        )

    def _decode_ics_text(self, value: str) -> str:
        decoded: List[str] = []
        index = 0

        while index < len(value):
            char = value[index]
            if char == "\\" and index + 1 < len(value):
                next_char = value[index + 1]
                if next_char in ("n", "N"):
                    decoded.append("\n")
                elif next_char == ",":
                    decoded.append(",")
                elif next_char == ";":
                    decoded.append(";")
                elif next_char == "\\":
                    decoded.append("\\")
                else:
                    decoded.append(next_char)
                index += 2
                continue

            decoded.append(char)
            index += 1

        return "".join(decoded)

    def _is_same_day(self, event: CalendarEvent, target_day: date) -> bool:
        day_start = datetime.combine(target_day, time.min, DISPLAY_TZ)
        next_day_start = day_start + timedelta(days=1)
        return event.start < next_day_start and event.end > day_start

    def _is_livestream_event(self, event: CalendarEvent) -> bool:
        if event.status == "CANCELLED":
            return False
        if event.all_day:
            return False
        if "live.bilibili.com" in event.url.lower():
            return True

        text = " ".join(
            part.lower()
            for part in (event.summary, event.location, event.categories)
            if part
        )
        has_live_keyword = any(keyword in text for keyword in LIVE_KEYWORDS)
        has_non_live_keyword = any(keyword in text for keyword in NON_LIVE_KEYWORDS)

        if has_live_keyword:
            return True
        if has_non_live_keyword:
            return False
        return True

    def _build_schedule_items(self, events: List[CalendarEvent]) -> List[ScheduleItem]:
        grouped: Dict[Tuple[datetime, str, str], ScheduleItem] = {}

        for event in events:
            hosts = self._extract_hosts(event)
            content = self._extract_content(event, hosts)
            label = self._classify_event_label(event, content)
            key = (event.start, content, label)

            if key not in grouped:
                grouped[key] = ScheduleItem(
                    start=event.start,
                    start_text=self._format_start_time(event.start),
                    hosts=[],
                    hosts_text="待确认",
                    content=content,
                    label=label,
                )

            item = grouped[key]
            for host in hosts:
                if host not in item.hosts:
                    item.hosts.append(host)
            item.hosts_text = " / ".join(item.hosts) if item.hosts else "待确认"

        items = list(grouped.values())
        items.sort(key=lambda item: item.start)
        return items

    async def _render_schedule_image(self, items: List[ScheduleItem]) -> str:
        try:
            return await asyncio.to_thread(self._render_schedule_image_local, items)
        except Exception:
            logger.exception("本地 Pillow 渲染失败")
            raise

    def _render_schedule_image_local(self, items: List[ScheduleItem]) -> str:
        from PIL import Image, ImageDraw, ImageFont

        today = datetime.now(DISPLAY_TZ).date()
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
            f"{today.strftime('%Y-%m-%d')} 今日直播",
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

    def _format_schedule_fallback(self, items: List[ScheduleItem]) -> str:
        today = datetime.now(DISPLAY_TZ).date().strftime("%Y-%m-%d")
        if not items:
            return f"{today} 今日暂无直播安排。"

        lines = [f"{today} 今日直播安排"]
        for item in items:
            lines.append(f"{item.start_text} {item.hosts_text} {item.content}")
        return "\n".join(lines)

    def _format_start_time(self, start: datetime) -> str:
        return start.strftime("%H:%M")

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
        avatar_names = ("贝拉", "嘉然", "乃琳", "心宜", "思诺")
        avatar_map: Dict[str, Path] = {}
        for name in avatar_names:
            path = PLUGIN_DIR / f"{name}.png"
            if path.exists():
                avatar_map[name] = path
        return avatar_map

    def _extract_hosts(self, event: CalendarEvent) -> List[str]:
        description_line = next(
            (line.strip() for line in event.description.splitlines() if line.strip()),
            "",
        )
        if "|" in description_line:
            _, raw_hosts = description_line.split("|", 1)
            hosts = [item for item in re.split(r"[ /、,，]+", raw_hosts.strip()) if item]
            if hosts:
                return hosts

        haystack = " ".join(
            part.lower() for part in (event.summary, event.description, event.location) if part
        )
        matched_hosts: List[str] = []

        for canonical, aliases in MEMBER_ALIASES:
            if any(alias.lower() in haystack for alias in aliases):
                matched_hosts.append(canonical)

        return matched_hosts

    def _extract_content(self, event: CalendarEvent, hosts: List[str]) -> str:
        summary = " ".join(event.summary.split())
        description_line = " ".join(event.description.splitlines()[0].split()) if event.description else ""

        candidate = summary or description_line or "直播内容待定"
        if ":" in candidate or "：" in candidate:
            separator = ":" if ":" in candidate else "："
            _, candidate = candidate.split(separator, 1)

        candidate = re.sub(r"【[^】]+】", " ", candidate)
        for canonical, aliases in MEMBER_ALIASES:
            if canonical not in hosts:
                continue
            for alias in aliases:
                candidate = candidate.replace(alias, "")
                candidate = candidate.replace(alias.upper(), "")
                candidate = candidate.replace(alias.title(), "")

        for token in ("直播", "开播", "直播安排", "今日", "今晚", "A-SOUL", "ASOUL", "突击", "线下"):
            candidate = candidate.replace(token, "")

        for separator in ("：", ":", "｜", "|", " - ", " / "):
            if separator in candidate:
                left, right = candidate.split(separator, 1)
                if len(left.strip()) <= 8 or any(host in left for host in hosts):
                    candidate = right
                    break

        cleaned = " ".join(candidate.replace("【", " ").replace("】", " ").split()).strip(" -|：:")
        if cleaned:
            return cleaned
        if summary:
            return summary
        if description_line:
            return description_line
        return "直播内容待定"

    def _classify_event_label(self, event: CalendarEvent, content: str) -> str:
        lowered = f"{event.summary} {content}".lower()
        if "突击" in event.summary:
            return "突击"
        if any(keyword in lowered for keyword in ("线下", "演唱会", "歌会", "music", "演唱")):
            return "演出"
        if any(keyword in lowered for keyword in ("节目", "综艺", "movie", "电影")):
            return "节目"
        if any(keyword in lowered for keyword in ("歌", "唱", "music", "演唱", "歌会")):
            return "歌会"
        if any(keyword in lowered for keyword in ("游戏", "联机", "fps", "mc", "minecraft")):
            return "游戏"
        if any(keyword in lowered for keyword in ("联动", "合作", "嘉宾", "同台")):
            return "联动"
        if any(keyword in lowered for keyword in ("杂谈", "聊天", "电台", "talk")):
            return "杂谈"
        if "2d" in lowered:
            return "2D"
        return "直播"

    async def terminate(self):
        """插件卸载时调用。"""
