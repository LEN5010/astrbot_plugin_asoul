import asyncio
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib import request
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

CALENDAR_URL = "https://asoul.love/calendar.ics"
CALENDAR_TTL = timedelta(minutes=10)
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")
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
SCHEDULE_IMAGE_TEMPLATE = """
<html>
<head>
  <meta charset="utf-8" />
  <style>
    :root {
      --bg: #f5efe6;
      --paper: rgba(255, 251, 245, 0.92);
      --paper-strong: #fffdf8;
      --ink: #201a17;
      --muted: #72675f;
      --line: rgba(32, 26, 23, 0.08);
      --accent: #d86f45;
      --accent-soft: rgba(216, 111, 69, 0.12);
      --shadow: 0 24px 80px rgba(70, 43, 22, 0.10);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      font-family: "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;
      background:
        radial-gradient(circle at top left, rgba(216, 111, 69, 0.18), transparent 34%),
        radial-gradient(circle at 85% 20%, rgba(68, 135, 120, 0.14), transparent 30%),
        linear-gradient(180deg, #f7f1e7 0%, #efe5d9 100%);
      color: var(--ink);
    }

    .page {
      width: 920px;
      padding: 40px 36px 32px;
    }

    .panel {
      position: relative;
      overflow: hidden;
      border: 1px solid rgba(255, 255, 255, 0.45);
      border-radius: 28px;
      background: var(--paper);
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
    }

    .panel::before {
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(135deg, rgba(255,255,255,0.45), transparent 36%),
        linear-gradient(180deg, transparent, rgba(255,255,255,0.22));
      pointer-events: none;
    }

    .header {
      position: relative;
      padding: 30px 30px 20px;
      border-bottom: 1px solid var(--line);
    }

    .eyebrow {
      display: inline-block;
      padding: 6px 12px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    h1 {
      margin: 14px 0 8px;
      font-size: 40px;
      line-height: 1.1;
      letter-spacing: -0.03em;
    }

    .subline {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      color: var(--muted);
      font-size: 16px;
    }

    .count {
      color: var(--ink);
      font-weight: 700;
    }

    .list {
      position: relative;
      padding: 18px 20px 22px;
    }

    .item {
      display: grid;
      grid-template-columns: 128px 1fr;
      gap: 18px;
      align-items: stretch;
      padding: 12px 10px;
    }

    .item + .item {
      border-top: 1px solid var(--line);
    }

    .time {
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 96px;
      border-radius: 22px;
      background: linear-gradient(180deg, rgba(255,255,255,0.9), rgba(255,248,241,0.95));
      border: 1px solid rgba(216, 111, 69, 0.10);
      color: var(--ink);
      font-size: 32px;
      font-weight: 800;
      letter-spacing: -0.04em;
    }

    .card {
      min-height: 96px;
      border-radius: 22px;
      padding: 18px 20px 16px;
      background: var(--paper-strong);
      border: 1px solid rgba(32, 26, 23, 0.06);
    }

    .meta {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      margin-bottom: 10px;
    }

    .tag {
      padding: 5px 10px;
      border-radius: 999px;
      background: #1f1b18;
      color: #f7f1e7;
      font-size: 13px;
      font-weight: 700;
      letter-spacing: 0.04em;
    }

    .hosts {
      color: var(--muted);
      font-size: 16px;
      font-weight: 600;
    }

    .content {
      font-size: 28px;
      line-height: 1.28;
      font-weight: 700;
      letter-spacing: -0.03em;
      word-break: break-word;
    }

    .empty {
      padding: 34px 28px 38px;
      text-align: center;
      color: var(--muted);
      font-size: 22px;
      line-height: 1.6;
    }

    .footer {
      padding: 0 30px 26px;
      color: rgba(114, 103, 95, 0.92);
      font-size: 13px;
      letter-spacing: 0.04em;
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="panel">
      <div class="header">
        <div class="eyebrow">A-SOUL LIVE</div>
        <h1>{{ date_text }} 今日直播</h1>
        <div class="subline">
          <span>{{ subtitle }}</span>
          <span class="count">{{ count_text }}</span>
        </div>
      </div>

      {% if items %}
      <div class="list">
        {% for item in items %}
        <div class="item">
          <div class="time">{{ item.start_text }}</div>
          <div class="card">
            <div class="meta">
              <span class="tag">{{ item.label }}</span>
              <span class="hosts">{{ item.hosts_text }}</span>
            </div>
            <div class="content">{{ item.content }}</div>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty">
        今天还没有查到直播安排。<br />
        晚点再发一次“直播数据”试试。
      </div>
      {% endif %}

      <div class="footer">Data source: asoul.love/calendar.ics</div>
    </div>
  </div>
</body>
</html>
"""


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
        today = datetime.now(DISPLAY_TZ).date()
        render_data = {
            "date_text": today.strftime("%Y-%m-%d"),
            "subtitle": "按 calendar.ics 清洗后的今日直播安排",
            "count_text": f"{len(items)} 条安排",
            "items": [
                {
                    "start_text": item.start_text,
                    "hosts_text": item.hosts_text,
                    "content": item.content,
                    "label": item.label,
                }
                for item in items
            ],
        }
        options = {
            "type": "png",
            "full_page": True,
            "animations": "disabled",
        }
        return await self.html_render(SCHEDULE_IMAGE_TEMPLATE, render_data, options=options)

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
