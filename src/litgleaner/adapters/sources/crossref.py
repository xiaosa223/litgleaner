"""Crossref：按期刊 ISSN 追踪新文章，按关键词检索。

Crossref 的关键词检索只按相关度排序、不支持布尔语法，所以每个词单独查一次，
再在本地按“至少命中一个词、不含排除词”过滤。窗口用 created 日期（DOI 首次注册），
不用 indexed 日期，避免老文章因元数据更新而反复出现。
文档：https://api.crossref.org/swagger-ui/index.html
"""

from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import Field

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.options import SourceOptions, Today, parse_options, utc_today
from litgleaner.adapters.common.query import matches_terms
from litgleaner.adapters.common.records import RecordBuilder, ensure_supported
from litgleaner.adapters.common.text import parse_date, strip_markup
from litgleaner.adapters.common.window import Window, next_cursor, window_from_cursor
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.query import IdentifierLookup, JournalWatch, KeywordSearch, Query
from litgleaner.ports.source import FetchResult

_FIELDS = "DOI,title,abstract,author,container-title,ISSN,issued,published,type"


class CrossrefOptions(SourceOptions):
    base_url: str = "https://api.crossref.org"
    rows: int = Field(default=200, gt=0, le=1000)
    rows_per_term: int = Field(default=200, gt=0, le=1000)
    min_interval_seconds: float = Field(default=0.2, ge=0)


class CrossrefSource:
    capabilities: frozenset[type] = frozenset({JournalWatch, KeywordSearch, IdentifierLookup})

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
        self._options = parse_options(CrossrefOptions, options, name)
        self._contact = contact_email
        self._today = today
        self._http = HttpClient(
            "Crossref",
            base_url=self._options.base_url,
            contact_email=contact_email,
            min_interval=self._options.min_interval_seconds,
            timeout=self._options.timeout_seconds,
            transport=transport,
        )

    def fetch(self, query: Query, cursor: str | None) -> FetchResult:
        ensure_supported(self.capabilities, query)
        if isinstance(query, IdentifierLookup):
            return self._lookup(query)
        options = self._options
        window = window_from_cursor(
            cursor,
            self._today(),
            lookback_days=options.lookback_days,
            overlap_days=options.overlap_days,
        )
        builder = RecordBuilder(self.name)
        self._http.reset_raw_log()
        if isinstance(query, JournalWatch):
            self._journal(query, window, builder)
        elif isinstance(query, KeywordSearch):
            self._keywords(query, window, builder)
        return FetchResult(
            records=builder.records,
            next_cursor=next_cursor(window),
            warnings=builder.warnings,
            truncated=builder.truncated,
            raw=self._http.raw_log(),
        )

    def _lookup(self, query: IdentifierLookup) -> FetchResult:
        builder = RecordBuilder(self.name)
        self._http.reset_raw_log()
        if query.doi:
            params = {"mailto": self._contact} if self._contact else None
            body = self._http.get_json(f"/works/{query.doi}", params, allow_missing=True)
            message = body.get("message") if isinstance(body, Mapping) else None
            if isinstance(message, Mapping):
                _add(builder, message)
            elif body is not None:
                raise SchemaChanged("Crossref 单篇查询返回缺少 message")
        return FetchResult(builder.records, None, builder.warnings, raw=self._http.raw_log())

    def _params(self, filters: list[str], window: Window, rows: int) -> dict[str, str]:
        dates = [
            f"from-created-date:{window.since.isoformat()}",
            f"until-created-date:{window.until.isoformat()}",
        ]
        params = {"filter": ",".join([*filters, *dates]), "rows": str(rows), "select": _FIELDS}
        if self._contact:
            params["mailto"] = self._contact
        return params

    def _journal(self, query: JournalWatch, window: Window, builder: RecordBuilder) -> None:
        issns = [f"issn:{issn}" for issn in sorted(query.issns)]
        params = self._params(issns, window, self._options.rows)
        page_cursor: str | None = "*"
        seen = 0
        while page_cursor:
            items, page_cursor = self._page({**params, "cursor": str(page_cursor)})
            for item in items:
                _add(builder, item)
            seen += len(items)
            if not items:
                break
            if seen >= self._options.max_records:
                builder.truncate(self._options.max_records)
                break

    def _keywords(self, query: KeywordSearch, window: Window, builder: RecordBuilder) -> None:
        params = self._params([], window, self._options.rows_per_term)
        for term in query.terms:
            items, _ = self._page({**params, "query.bibliographic": term})
            for item in items:
                title = _first(item.get("title"))
                abstract = strip_markup(item.get("abstract"))
                if title and matches_terms(query, title, abstract):
                    _add(builder, item)

    def _page(self, params: Mapping[str, str]) -> tuple[list[Mapping[str, Any]], str | None]:
        body = self._http.get_json("/works", params)
        message = body.get("message") if isinstance(body, Mapping) else None
        items = message.get("items") if isinstance(message, Mapping) else None
        if not isinstance(items, list):
            raise SchemaChanged("Crossref 返回缺少 message.items")
        next_page = message.get("next-cursor") if isinstance(message, Mapping) else None
        return [i for i in items if isinstance(i, Mapping)], next_page or None


def _first(value: Any) -> str | None:
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    return None


def _author(entry: Mapping[str, Any]) -> str:
    if entry.get("name"):
        return str(entry["name"])
    return " ".join(str(entry[k]) for k in ("given", "family") if entry.get(k))


def _date(item: Mapping[str, Any]) -> Any:
    for key in ("published", "issued"):
        holder = item.get(key)
        parts = holder.get("date-parts") if isinstance(holder, Mapping) else None
        if isinstance(parts, list) and parts and isinstance(parts[0], list):
            found = parse_date(parts[0])
            if found:
                return found
    return None


def _add(builder: RecordBuilder, item: Mapping[str, Any]) -> None:
    authors = [_author(a) for a in item.get("author") or [] if isinstance(a, Mapping)]
    builder.add(
        title=_first(item.get("title")) or "",
        abstract=strip_markup(item.get("abstract")),
        authors=tuple(a for a in authors if a),
        venue=_first(item.get("container-title")),
        issn=_first(item.get("ISSN")),
        published_at=_date(item),
        doi=item.get("DOI"),
    )
