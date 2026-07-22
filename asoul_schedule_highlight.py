import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from typing import Any, Iterable, List

from asoul_core import DISPLAY_TZ, ScheduleItem


KV_SCHEDULE_HIGHLIGHTS = "schedule_highlights"
DEFAULT_HIGHLIGHT_STYLE = "platinum"
HIGHLIGHT_STYLE_LABELS = {
    "pink": "粉色",
    "red": "红色",
    "platinum": "白金色",
}
HIGHLIGHT_STYLE_ALIASES = {
    "pink": "pink",
    "粉": "pink",
    "粉色": "pink",
    "red": "red",
    "红": "red",
    "红色": "red",
    "platinum": "platinum",
    "白金": "platinum",
    "白金色": "platinum",
}


@dataclass(frozen=True)
class ScheduleHighlightRecord:
    key: str
    target_date: str
    start_text: str
    content: str
    hosts_text: str
    created_at: int
    style: str = DEFAULT_HIGHLIGHT_STYLE


def normalize_schedule_highlight_style(raw_value: Any) -> str:
    return HIGHLIGHT_STYLE_ALIASES.get(
        str(raw_value or "").strip().lower(), ""
    )


def schedule_highlight_style_label(style: str) -> str:
    return HIGHLIGHT_STYLE_LABELS.get(style, HIGHLIGHT_STYLE_LABELS[DEFAULT_HIGHLIGHT_STYLE])


def build_schedule_highlight_key(item: ScheduleItem) -> str:
    start = item.start
    if start.tzinfo is None:
        start = start.replace(tzinfo=DISPLAY_TZ)
    else:
        start = start.astimezone(DISPLAY_TZ)
    identity = {
        "start": start.strftime("%Y-%m-%dT%H:%M"),
        "content": " ".join(item.content.split()),
        "label": " ".join(item.label.split()),
    }
    payload = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class ScheduleHighlightManager:
    def __init__(self, owner: Any) -> None:
        self._owner = owner
        self._records: dict[str, ScheduleHighlightRecord] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def apply(self, items: Iterable[ScheduleItem]) -> List[ScheduleItem]:
        await self._ensure_loaded()
        async with self._lock:
            active_records = dict(self._records)
        applied: List[ScheduleItem] = []
        for item in items:
            key = build_schedule_highlight_key(item)
            record = active_records.get(key)
            applied.append(
                replace(
                    item,
                    highlight_key=key,
                    highlighted=record is not None,
                    highlight_style=(
                        record.style if record is not None else ""
                    ),
                )
            )
        return applied

    async def mark(
        self,
        item: ScheduleItem,
        style: str = DEFAULT_HIGHLIGHT_STYLE,
    ) -> ScheduleHighlightRecord:
        await self._ensure_loaded()
        normalized_style = normalize_schedule_highlight_style(style) or DEFAULT_HIGHLIGHT_STYLE
        key = build_schedule_highlight_key(item)
        start = item.start
        if start.tzinfo is None:
            start = start.replace(tzinfo=DISPLAY_TZ)
        else:
            start = start.astimezone(DISPLAY_TZ)
        record = ScheduleHighlightRecord(
            key=key,
            target_date=start.date().isoformat(),
            start_text=item.start_text,
            content=item.content,
            hosts_text=item.hosts_text,
            created_at=int(datetime.now(DISPLAY_TZ).timestamp()),
            style=normalized_style,
        )
        async with self._lock:
            self._records[key] = record
            await self._persist_locked()
        return record

    async def unmark(self, item: ScheduleItem) -> bool:
        await self._ensure_loaded()
        return await self.unmark_key(build_schedule_highlight_key(item))

    async def unmark_key(self, key: str) -> bool:
        await self._ensure_loaded()
        normalized_key = str(key or "").strip()
        async with self._lock:
            removed = self._records.pop(normalized_key, None) is not None
            if removed:
                await self._persist_locked()
        return removed

    async def list_records(self) -> List[ScheduleHighlightRecord]:
        await self._ensure_loaded()
        async with self._lock:
            return sorted(
                self._records.values(),
                key=lambda record: (
                    record.target_date,
                    record.start_text,
                    record.content,
                ),
            )

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._lock:
            if self._loaded:
                return
            raw = await self._owner.get_kv_data(KV_SCHEDULE_HIGHLIGHTS, {})
            self._records = self._normalize_records(raw)
            self._loaded = True

    @staticmethod
    def _normalize_records(raw: Any) -> dict[str, ScheduleHighlightRecord]:
        if not isinstance(raw, dict):
            return {}
        source = raw.get("records", raw)
        if not isinstance(source, dict):
            return {}
        normalized: dict[str, ScheduleHighlightRecord] = {}
        for raw_key, raw_record in source.items():
            if not isinstance(raw_record, dict):
                continue
            key = str(raw_record.get("key") or raw_key or "").strip()
            target_date = str(raw_record.get("target_date") or "").strip()
            try:
                date.fromisoformat(target_date)
            except ValueError:
                continue
            if not key:
                continue
            try:
                created_at = max(0, int(raw_record.get("created_at") or 0))
            except (TypeError, ValueError):
                created_at = 0
            normalized[key] = ScheduleHighlightRecord(
                key=key,
                target_date=target_date,
                start_text=str(raw_record.get("start_text") or ""),
                content=str(raw_record.get("content") or ""),
                hosts_text=str(raw_record.get("hosts_text") or ""),
                created_at=created_at,
                style=(
                    normalize_schedule_highlight_style(raw_record.get("style"))
                    or DEFAULT_HIGHLIGHT_STYLE
                ),
            )
        return normalized

    async def _persist_locked(self) -> None:
        payload = {
            "version": 2,
            "records": {
                key: asdict(record) for key, record in self._records.items()
            },
        }
        await self._owner.put_kv_data(KV_SCHEDULE_HIGHLIGHTS, payload)
