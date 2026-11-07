"""增量抓取窗口。游标统一是 ISO 日期：上次抓到哪一天。

每次都和上次的窗口重叠几天，用来接住晚入库的记录；重复的记录由去重阶段处理。
"""

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class Window:
    since: date
    until: date


def window_from_cursor(
    cursor: str | None, today: date, *, lookback_days: int, overlap_days: int
) -> Window:
    last = _parse_cursor(cursor)
    if last is None:
        return Window(today - timedelta(days=lookback_days), today)
    since = min(last, today) - timedelta(days=overlap_days)
    return Window(since, today)


def next_cursor(window: Window) -> str:
    return window.until.isoformat()


def _parse_cursor(cursor: str | None) -> date | None:
    if not cursor:
        return None
    try:
        return date.fromisoformat(cursor)
    except ValueError:
        return None
