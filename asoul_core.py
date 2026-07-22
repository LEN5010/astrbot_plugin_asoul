from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple
from zoneinfo import ZoneInfo

PLUGIN_DIR = Path(__file__).resolve().parent
CALENDAR_URL = "https://asoul.love/calendar.ics"
CALENDAR_USER_AGENT = "asasfans"
CALENDAR_TTL = timedelta(minutes=30)
CALENDAR_CACHE_PATH = PLUGIN_DIR / "temp" / "calendar_cache.json"
DISPLAY_TZ = ZoneInfo("Asia/Shanghai")

TODAY_TRIGGER_TEXTS = {"今日直播"}
TOMORROW_TRIGGER_TEXTS = {"明日直播"}
THIS_WEEK_TRIGGER_TEXTS = {"本周直播"}
LIVE_TRIGGER_TEXTS = TODAY_TRIGGER_TEXTS | TOMORROW_TRIGGER_TEXTS | THIS_WEEK_TRIGGER_TEXTS
LIVE_REQUEST_FILTER_FLAG = "-a"
LIVE_REQUEST_FILTERED_HOSTS = frozenset({"心宜", "思诺"})
HELP_TRIGGER_TEXTS = {"/bot帮助", "bot帮助"}
HELP_MESSAGE = (
    "鸣潮bot请使用【ww帮助】获取图文\n"
    "自动签到请使用【ww登陆】，然后输入【ww开启自动签到】\n"
    "asoul推送请使用【今日直播】、【明日直播】或【本周直播】"
)
NO_NEXT_WEEK_SCHEDULE_TEXT = "还没有下周的直播排表哦"

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
ASOUL_CORE_MEMBERS = ["嘉然", "乃琳", "贝拉"]
AVATAR_NAMES = ("贝拉", "嘉然", "乃琳", "心宜", "思诺")


def parse_live_request(message_text: str) -> Optional[Tuple[str, bool]]:
    parts = message_text.strip().split()
    if len(parts) == 1 and parts[0] in LIVE_TRIGGER_TEXTS:
        return parts[0], False
    if (
        len(parts) == 2
        and parts[0] in LIVE_TRIGGER_TEXTS
        and parts[1] == LIVE_REQUEST_FILTER_FLAG
    ):
        return parts[0], True
    return None


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
    highlight_key: str = ""
    highlighted: bool = False
