"""OpenReview：按关键词检索会议和期刊的公开投稿。

使用 API v2 的 GET /notes/search（Elasticsearch 全文检索，不需要登录）。实测（2026-11-05）：
- 结果只按相关度排序，sort=cdate:desc 等参数被接受但不改变顺序，也没有日期过滤参数，
  所以只能按相关度翻 max_pages_per_term 页，再在本地按日期窗口过滤。热门词的新投稿可能排在
  检查范围之外，这是该接口的固有限制；翻页达到上限时会告警。
- term 不支持布尔语法：每个词单独检索，再用 matches_terms 在本地过滤。
- source=forum 只返回投稿本身（不含评审、评论）。DBLP 等导入的出版记录日期是占位值，跳过。
窗口按首次公开时间：有 odate（公开日期）用 odate，否则用 cdate（创建日期），都是毫秒时间戳。
文档：https://docs.openreview.net ，接口定义：https://api2.openreview.net/docs/api.yml
"""

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import Field

from litfactory.adapters.common.http import HttpClient
from litfactory.adapters.common.options import SourceOptions, Today, parse_options, utc_today
from litfactory.adapters.common.query import matches_terms
from litfactory.adapters.common.records import RecordBuilder, ensure_supported
from litfactory.adapters.common.text import strip_markup
from litfactory.adapters.common.window import Window, next_cursor, window_from_cursor
from litfactory.domain.errors import SchemaChanged
from litfactory.domain.query import KeywordSearch, Query
from litfactory.ports.source import FetchResult

_IMPORTED = ("DBLP.org/", "OpenReview.net/Archive/")


class OpenReviewOptions(SourceOptions):
    base_url: str = "https://api2.openreview.net"
    page_size: int = Field(default=200, gt=0, le=1000)
    max_pages_per_term: int = Field(default=3, gt=0, le=10)  # 检索最多翻到第 10000 条
    timeout_seconds: float = Field(default=90, gt=0)


class OpenReviewSource:
    capabilities: frozenset[type] = frozenset({KeywordSearch})

    def __init__(
        self,
        name: str,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        transport: httpx.BaseTransport | None = None,
        today: Today = utc_today,
    ) -> None:
        self.name = name
        self._options = parse_options(OpenReviewOptions, options, name)
        self._today = today
        self._http = HttpClient(
            "OpenReview",
            base_url=self._options.base_url,
            contact_email=contact_email,
            min_interval=self._options.min_interval_seconds,
            timeout=self._options.timeout_seconds,
            transport=transport,
        )

    def fetch(self, query: Query, cursor: str | None) -> FetchResult:
        ensure_supported(self.capabilities, query)
        options = self._options
        window = window_from_cursor(
            cursor,
            self._today(),
            lookback_days=options.lookback_days,
            overlap_days=options.overlap_days,
        )
        builder = RecordBuilder(self.name)
        self._http.reset_raw_log()
        if isinstance(query, KeywordSearch):
            for term in query.terms:
                if not self._term(term, query, window, builder):
                    break
        return FetchResult(
            records=builder.records,
            next_cursor=next_cursor(window),
            warnings=builder.warnings,
            truncated=builder.truncated,
            raw=self._http.raw_log(),
        )

    def _term(
        self, term: str, query: KeywordSearch, window: Window, builder: RecordBuilder
    ) -> bool:
        """检索一个词。达到 max_records 时返回 False，调用方不再检索后面的词。"""
        size, limit = self._options.page_size, self._options.max_records
        for page in range(self._options.max_pages_per_term):
            notes, total = self._page(term, page * size, size)
            for note in notes:
                if _wanted(note, query, window):
                    _add(builder, note)
                    if len(builder.records) >= limit:
                        builder.truncate(limit)
                        return False
            if len(notes) < size or (page + 1) * size >= total:
                return True
        builder.note(
            f"“{term}”达到翻页上限 {self._options.max_pages_per_term} 页，"
            "按相关度排在后面的结果没有检查"
        )
        return True

    def _page(self, term: str, offset: int, size: int) -> tuple[list[Mapping[str, Any]], int]:
        params = {"term": term, "source": "forum", "limit": str(size), "offset": str(offset)}
        body = self._http.get_json("/notes/search", params)
        notes = body.get("notes") if isinstance(body, Mapping) else None
        if not isinstance(notes, list):
            raise SchemaChanged("OpenReview 返回缺少 notes")
        count = body.get("count")
        total = count if isinstance(count, int) else offset + len(notes)
        return [n for n in notes if isinstance(n, Mapping)], total


def _value(note: Mapping[str, Any], field: str) -> Any:
    content = note.get("content")
    entry = content.get(field) if isinstance(content, Mapping) else None
    return entry.get("value") if isinstance(entry, Mapping) else None


def _day(milliseconds: Any) -> date | None:
    if not isinstance(milliseconds, int | float) or isinstance(milliseconds, bool):
        return None
    try:
        return datetime.fromtimestamp(milliseconds / 1000, UTC).date()
    except (OverflowError, OSError, ValueError):
        return None


def _public_day(note: Mapping[str, Any]) -> date | None:
    return _day(note.get("odate")) or _day(note.get("cdate"))


def _wanted(note: Mapping[str, Any], query: KeywordSearch, window: Window) -> bool:
    readers = note.get("readers")
    invitations = note.get("invitations")
    first = invitations[0] if isinstance(invitations, list) and invitations else ""
    if note.get("ddate") or not isinstance(readers, list) or "everyone" not in readers:
        return False
    if not isinstance(first, str) or first.startswith(_IMPORTED):
        return False
    day = _public_day(note)
    if day is None or not window.since <= day <= window.until:
        return False
    title = _value(note, "title")
    abstract = _value(note, "abstract")
    return isinstance(title, str) and matches_terms(
        query, title, abstract if isinstance(abstract, str) else None
    )


def _author(entry: Any) -> str | None:
    """多数会议的作者是字符串；部分研讨会是 {"fullname": ..., "username": ...} 对象。"""
    if isinstance(entry, Mapping):
        entry = entry.get("fullname")
    if not isinstance(entry, str):
        return None
    return " ".join(entry.split()) or None


def _add(builder: RecordBuilder, note: Mapping[str, Any]) -> None:
    authors = _value(note, "authors")
    names = (_author(a) for a in authors) if isinstance(authors, list) else ()
    venue = _value(note, "venue")
    abstract = _value(note, "abstract")
    builder.add(
        title=" ".join(str(_value(note, "title") or "").split()),
        abstract=strip_markup(abstract) if isinstance(abstract, str) else None,
        authors=tuple(name for name in names if name),
        venue=strip_markup(venue) if isinstance(venue, str) else None,
        published_at=_day(note.get("pdate")) or _public_day(note),
        external_ids={"openreview": note.get("forum") or note.get("id")},
    )
