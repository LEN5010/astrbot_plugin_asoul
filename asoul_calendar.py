import asyncio
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib import request
from zoneinfo import ZoneInfo

from asoul_core import (
    CALENDAR_TTL,
    CALENDAR_URL,
    DISPLAY_TZ,
    LIVE_KEYWORDS,
    NON_LIVE_KEYWORDS,
    CalendarEvent,
)

logger = logging.getLogger(__name__)


class CalendarRepository:
    def __init__(
        self,
        calendar_url: str = CALENDAR_URL,
        cache_ttl: timedelta = CALENDAR_TTL,
        display_tz: ZoneInfo = DISPLAY_TZ,
        user_agent: str = "astrbot-plugin-asoul/1.1.0",
    ) -> None:
        self._calendar_url = calendar_url
        self._cache_ttl = cache_ttl
        self._display_tz = display_tz
        self._user_agent = user_agent
        self._calendar_cache: List[CalendarEvent] = []
        self._calendar_cache_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._calendar_lock = asyncio.Lock()

    async def get_live_events_for_day(self, target_day: date) -> List[CalendarEvent]:
        calendar_events = await self._load_calendar_events()
        filtered_events = [
            event
            for event in calendar_events
            if self._is_same_day(event, target_day) and self._is_livestream_event(event)
        ]
        filtered_events.sort(key=lambda item: item.start)
        return filtered_events

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
            self._calendar_cache_expires_at = now + self._cache_ttl
            return list(events)

    def _download_calendar_text(self) -> str:
        req = request.Request(
            self._calendar_url,
            headers={"User-Agent": self._user_agent},
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
            return datetime.combine(parsed_date, time.min, self._display_tz)
        return parsed_date.astimezone(self._display_tz)

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
        tz = self._display_tz
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
        day_start = datetime.combine(target_day, time.min, self._display_tz)
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
