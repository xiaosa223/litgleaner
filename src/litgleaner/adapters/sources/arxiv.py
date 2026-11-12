"""arXiv：按关键词或作者检索新提交的预印本。

检索语法：关键词写成 all:"词"，多个词用 OR 连接，排除词用 ANDNOT；作者写成 au:"姓名"。
窗口：两道保险同时用。检索式里加 submittedDate:[YYYYMMDDHHMM TO YYYYMMDDHHMM] 范围（API 用户手册
记载的语法），同时按 submittedDate 降序翻页，一旦条目的首次提交日期早于窗口起点就停止。
范围语法被忽略或不准时，降序加提前停止仍能把结果限制在窗口内，最多多翻一页。
arXiv 要求请求间隔至少 3 秒，接口很慢（单次常要十几秒），所以默认间隔 3 秒、超时 90 秒。
出错时 arXiv 有时返回 HTTP 200 和一条标题为 Error 的条目，这里识别为 SchemaChanged。
文档：https://info.arxiv.org/help/api/user-manual.html
"""

import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
from pydantic import Field

from litgleaner.adapters.common.capture import captured_today
from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.options import SourceOptions, Today, parse_options, utc_today
from litgleaner.adapters.common.query import boolean_query
from litgleaner.adapters.common.records import RecordBuilder, ensure_supported
from litgleaner.adapters.common.text import parse_date, strip_markup
from litgleaner.adapters.common.window import Window, next_cursor, window_from_cursor
from litgleaner.domain.errors import SchemaChanged, TransientError
from litgleaner.domain.identity import normalize_arxiv_id
from litgleaner.domain.query import AuthorWatch, IdentifierLookup, KeywordSearch, Query
from litgleaner.ports.source import FetchResult

_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}
_ERROR_ID = "arxiv.org/api/errors"


class ArxivOptions(SourceOptions):
    base_url: str = "https://export.arxiv.org/api"
    page_size: int = Field(default=100, gt=0, le=2000)
    min_interval_seconds: float = Field(default=3.0, ge=3.0)
    timeout_seconds: float = Field(default=90, gt=0)


@dataclass(frozen=True)
class _Page:
    entries: list[ET.Element]
    total: int


class ArxivSource:
    capabilities: frozenset[type] = frozenset({KeywordSearch, AuthorWatch, IdentifierLookup})

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
        self._options = parse_options(ArxivOptions, options, name)
        self._today = captured_today(today)
        self._http = HttpClient(
            "arXiv",
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
        self._search(f"({_expression(query)}) AND {_date_range(window)}", window, builder)
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
        if query.arxiv_id:
            params = {"id_list": query.arxiv_id, "max_results": "1"}
            page = _parse_feed(self._http.get_text("/query", params) or "")
            for entry in page.entries[:1]:
                _add(builder, entry)
        return FetchResult(builder.records, None, builder.warnings, raw=self._http.raw_log())

    def _search(self, expression: str, window: Window, builder: RecordBuilder) -> None:
        size, limit = self._options.page_size, self._options.max_records
        seen = 0
        while seen < limit:
            page = self._page(expression, seen, min(size, limit - seen))
            if not page.entries and seen < page.total:
                # arXiv 偶尔在还有结果时返回空页。当作临时故障，避免游标前进后漏掉记录。
                raise TransientError(f"arXiv 在第 {seen} 条处返回空页（共 {page.total} 条）")
            for entry in page.entries[: limit - seen]:
                submitted = parse_date(_text(entry, "atom:published"))
                if submitted is not None and submitted < window.since:
                    return
                _add(builder, entry)
                seen += 1
            if seen >= page.total:
                return
        builder.truncate(limit)

    def _page(self, expression: str, start: int, size: int) -> _Page:
        params = {
            "search_query": expression,
            "start": str(start),
            "max_results": str(size),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        return _parse_feed(self._http.get_text("/query", params) or "")


def _phrase(prefix: str) -> Callable[[str], str]:
    def quote(term: str) -> str:
        return f'{prefix}:"' + " ".join(term.replace('"', " ").split()) + '"'

    return quote


def _expression(query: Query) -> str:
    if isinstance(query, KeywordSearch):
        return boolean_query(query, phrase=_phrase("all"), not_op="ANDNOT")
    if isinstance(query, AuthorWatch):
        return " OR ".join(_phrase("au")(name) for name in query.names)
    raise ValueError(f"不支持的查询：{type(query).__name__}")


def _date_range(window: Window) -> str:
    return f"submittedDate:[{_stamp(window.since)}0000 TO {_stamp(window.until)}2359]"


def _stamp(day: date) -> str:
    return day.strftime("%Y%m%d")


def _parse_feed(text: str) -> _Page:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise SchemaChanged("arXiv 返回的不是合法的 Atom XML") from error
    if root.tag != f"{{{_NS['atom']}}}feed":
        raise SchemaChanged(f"arXiv 返回的根元素不是 Atom feed：{root.tag}")
    entries = root.findall("atom:entry", _NS)
    for entry in entries:
        if _ERROR_ID in (_text(entry, "atom:id") or ""):
            detail = strip_markup(_text(entry, "atom:summary")) or "未知错误"
            raise SchemaChanged(f"arXiv 拒绝了检索请求：{detail}")
    total = _text(root, "opensearch:totalResults")
    if total is None or not _is_number(total.strip()):
        raise SchemaChanged("arXiv 返回缺少 opensearch:totalResults")
    return _Page(entries, int(total))


def _text(element: ET.Element, path: str) -> str | None:
    found = element.find(path, _NS)
    return found.text if found is not None and found.text else None


def _venue(entry: ET.Element) -> str | None:
    journal = strip_markup(_text(entry, "arxiv:journal_ref"))
    if journal:
        return journal
    primary = entry.find("arxiv:primary_category", _NS)
    term = primary.get("term") if primary is not None else None
    return f"arXiv {term}" if term else "arXiv"


def _add(builder: RecordBuilder, entry: ET.Element) -> None:
    names = (strip_markup(_text(a, "atom:name")) for a in entry.findall("atom:author", _NS))
    builder.add(
        title=strip_markup(_text(entry, "atom:title")) or "",
        abstract=strip_markup(_text(entry, "atom:summary")),
        authors=tuple(name for name in names if name),
        venue=_venue(entry),
        published_at=parse_date(_text(entry, "atom:published")),
        doi=_text(entry, "arxiv:doi"),
        arxiv_id=normalize_arxiv_id(_text(entry, "atom:id")),
    )


def _is_number(text: str) -> bool:
    """只认 ASCII 数字。str.isdigit() 对“²”之类也返回 True，随后 int() 会抛错。"""
    return text.isascii() and text.isdecimal()
