"""每个检索词近一年能搜到多少篇（第 7.2 节），帮助判断太宽或太窄。

只向支持计数的来源发检索请求，不调用模型、不写文件。一组同义词一次请求；某个词查失败时只标出这个词，
被限流或总时长超过一分钟时剩下的词不再请求。
"""

import logging
import time
from dataclasses import dataclass
from datetime import date, timedelta

from litgleaner.application.context import AppContext
from litgleaner.domain.errors import InvalidRequest, LitGleanerError, RateLimited
from litgleaner.domain.query import TermCount

logger = logging.getLogger(__name__)

WINDOW_DAYS = 365
MAX_ROWS = 20
DEADLINE_SECONDS = 60  # 超过后剩下的行不再请求


@dataclass(frozen=True)
class TermRow:
    term: str
    synonyms: tuple[str, ...]


@dataclass(frozen=True)
class TermHits:
    term: str
    hits: int | None
    error: str | None  # source_unavailable：来源暂时查不到，稍后再试


@dataclass(frozen=True)
class TermCounts:
    source: str
    since: date
    until: date
    counts: tuple[TermHits, ...]


def _queries(
    rows: tuple[TermRow, ...], exclude: tuple[str, ...], since: date, until: date
) -> list[TermCount]:
    """先把每一行都校验完，有一行不合法就一次请求也不发。"""
    if not rows or len(rows) > MAX_ROWS:
        raise InvalidRequest("query_invalid")
    try:
        return [
            TermCount(terms=(row.term, *row.synonyms), exclude=exclude, since=since, until=until)
            for row in rows
        ]
    except ValueError:
        raise InvalidRequest("query_invalid") from None


def count_terms(ctx: AppContext, rows: tuple[TermRow, ...], exclude: tuple[str, ...]) -> TermCounts:
    capable = sorted(name for name, s in ctx.sources.items() if TermCount in s.capabilities)
    if not capable:
        raise InvalidRequest("term_count_unavailable")
    source = ctx.sources[capable[0]]
    until = ctx.clock().astimezone(ctx.settings.timezone).date()
    since = until - timedelta(days=WINDOW_DAYS)
    queries = _queries(rows, exclude, since, until)
    deadline = time.monotonic() + DEADLINE_SECONDS
    hits: list[int | None] = []
    for query in queries:
        if time.monotonic() > deadline:
            break  # 剩下的标为暂时查不到，不让页面一直转
        try:
            hits.append(source.fetch(query, None).total)
        except LitGleanerError as error:
            logger.warning("检索词计数失败：%s", type(error).__name__)
            hits.append(None)
            if isinstance(error, RateLimited):
                break  # 被限流时后面的请求也会失败，还多占用量
    hits.extend([None] * (len(queries) - len(hits)))
    counts = tuple(
        TermHits(row.term, value, None if value is not None else "source_unavailable")
        for row, value in zip(rows, hits, strict=True)
    )
    return TermCounts(source.name, since, until, counts)
