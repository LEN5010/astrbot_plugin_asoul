import re
from datetime import date, datetime
from typing import Dict, List, Tuple

from asoul_core import ASOUL_CORE_MEMBERS, MEMBER_ALIASES, CalendarEvent, ScheduleItem


class ScheduleService:
    def build_schedule_items(self, events: List[CalendarEvent]) -> List[ScheduleItem]:
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

    def format_schedule_fallback(
        self,
        items: List[ScheduleItem],
        target_day: date,
        title_text: str,
    ) -> str:
        day_text = target_day.strftime("%Y-%m-%d")
        if not items:
            return f"{day_text} 暂无{title_text}安排。"

        lines = [f"{day_text} {title_text}安排"]
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
            raw_hosts = raw_hosts.strip()
            if self._contains_asoul_group(raw_hosts):
                return list(ASOUL_CORE_MEMBERS)
            hosts = [item for item in re.split(r"[ /、,，&＆+和]+", raw_hosts) if item]
            if hosts:
                return hosts

        haystack = " ".join(
            part.lower() for part in (event.summary, event.description, event.location) if part
        )
        if self._contains_asoul_group(haystack):
            return list(ASOUL_CORE_MEMBERS)

        matched_hosts: List[str] = []

        for canonical, aliases in MEMBER_ALIASES:
            if canonical == "A-SOUL":
                continue
            if any(alias.lower() in haystack for alias in aliases):
                matched_hosts.append(canonical)

        return matched_hosts

    def _contains_asoul_group(self, text: str) -> bool:
        lowered = text.lower()
        return any(alias in lowered for alias in ("a-soul", "asoul", "一个魂"))

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
