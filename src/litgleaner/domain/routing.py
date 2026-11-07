"""推送路由：什么内容、什么时候、发到哪个渠道。"""

import re
from collections.abc import Mapping
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.topic import Topic

_DAILY = re.compile(r"^daily (\d{2}):(\d{2})$")
_WEEKLY = re.compile(r"^weekly (mon|tue|wed|thu|fri|sat|sun) (\d{2}):(\d{2})$")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ALL_TOPICS = "*"
PUBLIC_TOPICS = "public_topics"


def _parse_clock(hour: str, minute: str) -> time:
    return time(int(hour), int(minute))


class Route(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    topic: str
    channel: str
    schedule: str
    tiers: tuple[str, ...] = Field(min_length=1)
    limit: int = Field(gt=0)

    @field_validator("schedule")
    @classmethod
    def _valid_schedule(cls, value: str) -> str:
        if daily := _DAILY.match(value):
            _parse_clock(*daily.groups())
        elif weekly := _WEEKLY.match(value):
            _parse_clock(weekly.group(2), weekly.group(3))
        else:
            raise ValueError(f"无法识别的时间：{value}，示例：daily 08:30、weekly mon 09:00")
        return value


def _daily_slot(match: re.Match[str], now: datetime) -> str | None:
    due = now.time() >= _parse_clock(*match.groups())
    return now.date().isoformat() if due else None


def _weekly_slot(match: re.Match[str], now: datetime) -> str:
    """返回最近一个已到达的推送时刻所属的周。"""
    weekday = _WEEKDAYS.index(match.group(1))
    days_back = (now.weekday() - weekday) % 7
    scheduled = (now - timedelta(days=days_back)).date()
    if days_back == 0 and now.time() < _parse_clock(match.group(2), match.group(3)):
        scheduled -= timedelta(days=7)
    year, week, _ = scheduled.isocalendar()
    return f"{year}-W{week:02d}"


def schedule_slot(schedule: str, now: datetime, zone: ZoneInfo) -> str | None:
    """返回当前时刻已经到期的周期标识；还没到点返回 None。同一周期只推送一次。"""
    local = now.astimezone(zone)
    if daily := _DAILY.match(schedule):
        return _daily_slot(daily, local)
    if weekly := _WEEKLY.match(schedule):
        return _weekly_slot(weekly, local)
    raise ValueError(f"无法识别的时间：{schedule}")


def expand_topics(selector: str, topics: Mapping[str, Topic]) -> tuple[Topic, ...]:
    ordered = [topics[key] for key in sorted(topics)]
    if selector == ALL_TOPICS:
        return tuple(ordered)
    if selector == PUBLIC_TOPICS:
        return tuple(topic for topic in ordered if topic.public)
    return (topics[selector],) if selector in topics else ()
