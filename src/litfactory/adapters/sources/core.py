"""CORE：全球开放获取机构知识库的聚合检索。

CORE 从世界各地上万个大学和机构知识库、预印本库、开放期刊收割元数据和全文，
覆盖大量出版商索引里看不到的论文：非英语地区的本地期刊、学位论文、机构收录的作者稿。
对关注拉美、非洲、亚洲、东欧等地区研究的用户，这是商业索引之外最重要的补充来源。

检索语言（https://api.core.ac.uk/docs/v3 “CORE API Query Language”）支持 AND / OR、
括号、字段查询（title:"..."、abstract:"..."）和数值范围（yearPublished>=2026）。
文档没有列出 NOT，所以排除词在本地按标题和摘要过滤。

窗口：文档里的 createdDate（入库时间）现在不能用于过滤或排序（2026-11-05 实测，
后端报“找不到属性 createdDate”），日期字段的范围比较也会因类型不符失败。所以服务端只按
yearPublished 限定年份，按 publishedDate 倒序取，本地按 publishedDate 落在窗口内过滤，
翻到早于窗口的记录就停止。publishedDate 偶尔是占位的未来日期，会被窗口过滤掉。

需要免费注册的 API key（Authorization: Bearer）。个人 key 每分钟最多 25 次，默认间隔 3 秒。
"""

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx
from pydantic import Field

from litfactory.adapters.common.http import HttpClient
from litfactory.adapters.common.options import SourceOptions, Today, parse_options, utc_today
from litfactory.adapters.common.query import boolean_query, matches_terms
from litfactory.adapters.common.records import RecordBuilder, ensure_supported
from litfactory.adapters.common.text import parse_date, strip_markup
from litfactory.adapters.common.window import Window, next_cursor, window_from_cursor
from litfactory.domain.errors import AuthFailed, SchemaChanged
from litfactory.domain.query import KeywordSearch, Query
from litfactory.ports.source import FetchResult

_ISSN = re.compile(r"\b(\d{4}-\d{3}[\dXx])\b")


class CoreOptions(SourceOptions):
    base_url: str = "https://api.core.ac.uk/v3"
    page_size: int = Field(default=100, gt=0, le=100)
    min_interval_seconds: float = Field(default=3.0, ge=0)


_MISSING_KEY = "CORE 需要 API key：请免费注册（https://core.ac.uk/services/api）并设置 CORE_API_KEY"


class CoreSource:
    capabilities: frozenset[type] = frozenset({KeywordSearch})

    def __init__(
        self,
        name: str,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        today: Today = utc_today,
    ) -> None:
        self.name = name
        self._options = parse_options(CoreOptions, options, name)
        self._today = today
        self._has_key = bool(api_key)
        self._http = HttpClient(
            "CORE",
            base_url=self._options.base_url,
            contact_email=contact_email,
            secrets=[api_key],
            headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
            min_interval=self._options.min_interval_seconds,
            timeout=self._options.timeout_seconds,
            transport=transport,
        )

    def fetch(self, query: Query, cursor: str | None) -> FetchResult:
        ensure_supported(self.capabilities, query)
        if not self._has_key:
            raise AuthFailed(_MISSING_KEY)
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
            self._keywords(query, window, builder)
        return FetchResult(
            records=builder.records,
            next_cursor=next_cursor(window),
            warnings=builder.warnings,
            truncated=builder.truncated,
            raw=self._http.raw_log(),
        )

    def _keywords(self, query: KeywordSearch, window: Window, builder: RecordBuilder) -> None:
        params = {
            "q": search_query(query, window),
            "sort": "publishedDate:desc",
            "limit": str(self._options.page_size),
        }
        excluded = KeywordSearch(terms=query.exclude) if query.exclude else None
        offset = 0
        while True:
            works, total = self._page({**params, "offset": str(offset)})
            for work in works:
                if _wanted(work, window, excluded):
                    _add(builder, work)
            offset += len(works)
            short = len(works) < self._options.page_size
            if short or offset >= total or _reached_older(works, window):
                return
            if offset >= self._options.max_records:
                builder.truncate(self._options.max_records)
                return

    def _page(self, params: Mapping[str, str]) -> tuple[list[Mapping[str, Any]], int]:
        body = self._http.get_json("/search/works/", params)
        results = body.get("results") if isinstance(body, Mapping) else None
        if not isinstance(results, list):
            raise SchemaChanged("CORE 返回缺少 results 列表")
        total = body.get("totalHits")
        works = [item for item in results if isinstance(item, Mapping)]
        return works, total if isinstance(total, int) else 0


def _field_phrase(term: str) -> str:
    text = '"' + term.replace('"', " ").strip() + '"'
    return f"(title:{text} OR abstract:{text})"


def search_query(query: KeywordSearch, window: Window) -> str:
    terms = boolean_query(KeywordSearch(terms=query.terms), phrase=_field_phrase)
    years = f"yearPublished>={window.since.year} AND yearPublished<={window.until.year}"
    return f"{terms} AND {years}"


def _published(work: Mapping[str, Any]) -> date | None:
    value = work.get("publishedDate")
    return parse_date(value) if isinstance(value, str) else None


def _reached_older(works: list[Mapping[str, Any]], window: Window) -> bool:
    """结果按 publishedDate 倒序：出现早于窗口或没有日期的记录，后面就都不用看了。"""
    for work in works:
        published = _published(work)
        if published is None or published < window.since:
            return True
    return False


def _wanted(work: Mapping[str, Any], window: Window, excluded: KeywordSearch | None) -> bool:
    published = _published(work)
    if published is None or not window.since <= published <= window.until:
        return False
    if excluded is None:
        return True
    title = str(work.get("title") or "")
    return not matches_terms(excluded, title, strip_markup(work.get("abstract")))


def _venue_and_issn(work: Mapping[str, Any]) -> tuple[str | None, str | None]:
    for journal in work.get("journals") or []:
        if not isinstance(journal, Mapping):
            continue
        identifiers = " ".join(str(i) for i in journal.get("identifiers") or [])
        match = _ISSN.search(identifiers)
        title = journal.get("title")
        if title or match:
            return (str(title) if title else None), (match.group(1).upper() if match else None)
    return None, None


def _add(builder: RecordBuilder, work: Mapping[str, Any]) -> None:
    authors = [a.get("name") for a in work.get("authors") or [] if isinstance(a, Mapping)]
    venue, issn = _venue_and_issn(work)
    builder.add(
        title=work.get("title") or "",
        abstract=strip_markup(work.get("abstract")),
        authors=tuple(str(a).strip() for a in authors if a and str(a).strip()),
        venue=venue,
        issn=issn,
        published_at=_published(work),
        doi=work.get("doi") or None,
        arxiv_id=work.get("arxivId") or None,
        external_ids={"core": work.get("id"), "pmid": work.get("pubmedId")},
    )
