"""Europe PMC：按关键词、期刊 ISSN、作者追踪生物医学文献，同时覆盖预印本。

Europe PMC 除了收录 PubMed/PMC 的期刊文章，还收录 bioRxiv、medRxiv、Research Square、
SSRN 等服务器的预印本（来源代码 SRC:PPR），这是本系统覆盖生物医学预印本的途径。
预印本没有 PMID，用键名 europepmc、值 "{source}:{id}"（如 "PPR:PPR123"）标识。

检索支持布尔语法和引号短语。窗口用 FIRST_IDATE（首次在 Europe PMC 建立索引的日期），
晚入库的老文章也能接住；重叠窗口带来的重复由去重阶段处理。分页用 cursorMark 深分页。
文档：https://europepmc.org/RestfulWebService 、https://europepmc.org/searchsyntax
"""

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx
from pydantic import Field

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.options import SourceOptions, Today, parse_options, utc_today
from litgleaner.adapters.common.query import boolean_query
from litgleaner.adapters.common.records import RecordBuilder, ensure_supported
from litgleaner.adapters.common.text import parse_date, strip_markup
from litgleaner.adapters.common.window import Window, next_cursor, window_from_cursor
from litgleaner.domain.errors import SchemaChanged, TransientError
from litgleaner.domain.query import AuthorWatch, JournalWatch, KeywordSearch, Query
from litgleaner.ports.source import FetchResult

_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_ABSTRACT_HEADING = re.compile(r"^\s*<title>\s*Abstract\s*</title>", re.IGNORECASE)


class EuropePmcOptions(SourceOptions):
    base_url: str = _BASE_URL
    page_size: int = Field(default=200, gt=0, le=1000)
    min_interval_seconds: float = Field(default=0.5, ge=0)


def _search_results(body: Any) -> list[Mapping[str, Any]]:
    """取出 search 响应里的记录。服务端故障有时以 200 + errCode 返回。"""
    if isinstance(body, Mapping) and body.get("errCode"):
        message = f"Europe PMC 返回错误 {body.get('errCode')}：{str(body.get('errMsg'))[:200]}"
        if str(body.get("errCode")).startswith("5"):
            raise TransientError(message)
        raise SchemaChanged(message)
    result_list = body.get("resultList") if isinstance(body, Mapping) else None
    results = result_list.get("result") if isinstance(result_list, Mapping) else None
    if not isinstance(results, list):
        raise SchemaChanged("Europe PMC 返回缺少 resultList.result")
    return [item for item in results if isinstance(item, Mapping)]


def _quoted(value: str) -> str:
    return '"' + value.replace('"', " ").strip() + '"'


class EuropePmcSource:
    capabilities: frozenset[type] = frozenset({KeywordSearch, JournalWatch, AuthorWatch})

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
        self._options = parse_options(EuropePmcOptions, options, name)
        self._contact = contact_email
        self._today = today
        self._http = HttpClient(
            "Europe PMC",
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
        self._collect(_query_text(query, window), builder)
        return FetchResult(
            records=builder.records,
            next_cursor=next_cursor(window),
            warnings=builder.warnings,
            truncated=builder.truncated,
            raw=self._http.raw_log(),
        )

    def _collect(self, text: str, builder: RecordBuilder) -> None:
        limit = self._options.max_records
        params = {
            "query": text,
            "format": "json",
            "resultType": "core",
            "pageSize": str(min(self._options.page_size, limit)),
        }
        if self._contact:
            params["email"] = self._contact
        mark, seen = "*", 0
        while True:
            body = self._http.get_json("/search", {**params, "cursorMark": mark})
            items = _search_results(body)
            for item in items[: limit - seen]:
                _add(builder, item)
            seen += len(items)
            total = _int(body.get("hitCount"))
            if seen >= limit and total > limit:
                builder.truncate(limit)
            following = body.get("nextCursorMark")
            if not items or seen >= min(limit, total) or not following or following == mark:
                return
            mark = str(following)


def _query_text(query: Query, window: Window) -> str:
    if isinstance(query, KeywordSearch):
        clause = boolean_query(query, phrase=_quoted)
    elif isinstance(query, JournalWatch):
        clause = "(" + " OR ".join(f"ISSN:{_quoted(i)}" for i in sorted(query.issns)) + ")"
    elif isinstance(query, AuthorWatch):
        clause = "(" + " OR ".join(f"AUTH:{_quoted(n)}" for n in query.names) + ")"
    else:
        raise ValueError(f"不支持的查询：{type(query).__name__}")
    dates = f"FIRST_IDATE:[{window.since.isoformat()} TO {window.until.isoformat()}]"
    return f"{clause} AND {dates}"


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _title(value: Any) -> str | None:
    """标题里的标签常以转义形式出现（&lt;i&gt;），strip_markup 会先还原再去标签。"""
    return strip_markup(value)


def _abstract(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return strip_markup(_ABSTRACT_HEADING.sub("", value))


def _author(entry: Mapping[str, Any]) -> str | None:
    if entry.get("collectiveName"):
        return str(entry["collectiveName"])
    names = [str(entry[key]).strip() for key in ("firstName", "lastName") if entry.get(key)]
    return " ".join(names) or (str(entry["fullName"]) if entry.get("fullName") else None)


def _authors(item: Mapping[str, Any]) -> tuple[str, ...]:
    author_list = item.get("authorList")
    entries = author_list.get("author") if isinstance(author_list, Mapping) else None
    names = (_author(e) for e in entries or [] if isinstance(e, Mapping))
    return tuple(name for name in names if name)


def _journal(item: Mapping[str, Any]) -> Mapping[str, Any]:
    info = item.get("journalInfo")
    journal = info.get("journal") if isinstance(info, Mapping) else None
    return journal if isinstance(journal, Mapping) else {}


def _venue(item: Mapping[str, Any]) -> str | None:
    title = _journal(item).get("title")
    if title:
        return str(title)
    book = item.get("bookOrReportDetails")
    publisher = book.get("publisher") if isinstance(book, Mapping) else None
    return str(publisher) if publisher else None


def _date(item: Mapping[str, Any]) -> date | None:
    for key in ("firstPublicationDate", "electronicPublicationDate", "pubYear"):
        value = item.get(key)
        if isinstance(value, str) and (found := parse_date(value)):
            return found
    return None


def _external_ids(item: Mapping[str, Any]) -> dict[str, str | None]:
    ids: dict[str, str | None] = {"pmid": item.get("pmid"), "pmcid": item.get("pmcid")}
    if not item.get("pmid") and item.get("source") and item.get("id"):
        ids["europepmc"] = f"{item['source']}:{item['id']}"
    return ids


def _add(builder: RecordBuilder, item: Mapping[str, Any]) -> None:
    journal = _journal(item)
    issn = journal.get("issn") or journal.get("essn")
    builder.add(
        title=_title(item.get("title")) or "",
        abstract=_abstract(item.get("abstractText")),
        authors=_authors(item),
        venue=_venue(item),
        issn=str(issn) if issn else None,
        published_at=_date(item),
        doi=item.get("doi"),
        external_ids=_external_ids(item),
    )
