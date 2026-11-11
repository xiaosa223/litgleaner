"""用来源类型、课题内引文和阅读价值排列阅读路线，不调用模型。"""

import calendar
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Literal

from litgleaner.domain.paper import Paper

RouteKind = Literal["reviews", "foundations", "representatives", "recent"]
_REVIEW_TYPES = frozenset({"review", "survey", "systematic review", "literature review"})


@dataclass(frozen=True)
class RouteCandidate:
    paper: Paper
    topic_citations: int = 0
    reading_value: int | None = None


@dataclass(frozen=True)
class RouteGroup:
    kind: RouteKind
    paper_ids: tuple[str, ...]


@dataclass(frozen=True)
class RouteOrder:
    groups: tuple[RouteGroup, ...]
    ungrouped: tuple[str, ...]


def recent_since(today: date) -> date:
    """两年采用日历周年；闰日落到二月底。"""
    year = today.year - 2
    day = min(today.day, calendar.monthrange(year, today.month)[1])
    return date(year, today.month, day)


def _review(item: RouteCandidate) -> bool:
    types = {
        " ".join(unicodedata.normalize("NFKC", value).casefold().replace("-", " ").split())
        for value in item.paper.publication_types
    }
    return bool(types & _REVIEW_TYPES)


def _published(item: RouteCandidate) -> date | None:
    try:
        return date.fromisoformat(item.paper.published_at or "")
    except ValueError:
        return None


def _recent(item: RouteCandidate, today: date) -> bool:
    published = _published(item)
    return published is not None and recent_since(today) <= published <= today


def order_route(
    candidates: tuple[RouteCandidate, ...], today: date, *, limit_per_group: int = 10
) -> RouteOrder:
    """按四种可解释依据依次选文，每篇只出现一次，未选中的论文明确列出。"""
    if not 1 <= limit_per_group <= 100:
        raise ValueError("每组篇数必须在 1 到 100 之间")
    ids = [item.paper.id for item in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("阅读路线候选论文编号不能重复")
    if any(item.topic_citations < 0 for item in candidates):
        raise ValueError("课题内引用数不能为负数")
    ranked: tuple[tuple[RouteKind, list[RouteCandidate]], ...] = (
        ("reviews", sorted((c for c in candidates if _review(c)), key=_recent_key)),
        (
            "foundations",
            sorted(
                (c for c in candidates if c.topic_citations > 0),
                key=lambda c: (-c.topic_citations, _published(c) or date.max, c.paper.id),
            ),
        ),
        (
            "representatives",
            sorted((c for c in candidates if c.reading_value is not None), key=_score_key),
        ),
        ("recent", sorted((c for c in candidates if _recent(c, today)), key=_recent_key)),
    )
    return _choose(ranked, candidates, limit_per_group)


def _recent_key(item: RouteCandidate) -> tuple[int, str]:
    return (-(_published(item) or date.min).toordinal(), item.paper.id)


def _score_key(item: RouteCandidate) -> tuple[int, int, str]:
    return (-(item.reading_value or 0), *_recent_key(item))


def _choose(
    ranked: tuple[tuple[RouteKind, list[RouteCandidate]], ...],
    candidates: tuple[RouteCandidate, ...],
    limit: int,
) -> RouteOrder:
    used: set[str] = set()
    groups: list[RouteGroup] = []
    for kind, ordered in ranked:
        picked = tuple(item.paper.id for item in ordered if item.paper.id not in used)[:limit]
        groups.append(RouteGroup(kind, picked))
        used.update(picked)
    return RouteOrder(
        tuple(groups), tuple(sorted(c.paper.id for c in candidates if c.paper.id not in used))
    )
