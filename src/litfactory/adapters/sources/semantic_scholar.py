"""Semantic Scholar：关键词检索、引用追踪、作者追踪。

- 关键词用 /paper/search/bulk：支持布尔语法（| 表示或，紧贴括号的 - 表示排除，引号表示短语），
  用 publicationDateOrYear 限定窗口，按 token 翻页。
- 引用追踪用 /paper/DOI:{doi}/citations，作者追踪先 /author/search 再 /author/{id}/papers，
  都按 offset/limit 翻页。这两个接口也接受 publicationDateOrYear，服务端先缩小范围，
  本地再按 publicationDate（缺失时按年份的 1 月 1 日，与服务端一致）过滤一遍。
- Semantic Scholar 的作者消歧会把同一个人拆成多个档案，所以只有首个候选的姓名与查询完全一致、
  且其他候选里没有同名档案时才采用，否则告警跳过，不猜。
- 没有 key 时走共享限流池，经常 429，默认请求间隔放大到 3 秒；有 key 时 1 秒。
文档：https://api.semanticscholar.org/api-docs/graph
"""

import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import Field

from litfactory.adapters.common.http import HttpClient
from litfactory.adapters.common.options import SourceOptions, Today, parse_options, utc_today
from litfactory.adapters.common.query import boolean_query
from litfactory.adapters.common.records import RecordBuilder, ensure_supported
from litfactory.adapters.common.text import parse_date, strip_markup
from litfactory.adapters.common.window import Window, next_cursor, window_from_cursor
from litfactory.domain.errors import SchemaChanged
from litfactory.domain.query import AuthorWatch, CitationWatch, KeywordSearch, Query
from litfactory.ports.source import FetchResult

_FIELDS = "paperId,title,abstract,authors,venue,publicationDate,year,externalIds,journal"
_INTERVAL_WITH_KEY = 1.0
_INTERVAL_WITHOUT_KEY = 3.0


class SemanticScholarOptions(SourceOptions):
    base_url: str = "https://api.semanticscholar.org/graph/v1"
    page_size: int = Field(default=100, gt=0, le=1000)
    author_candidates: int = Field(default=10, gt=0, le=100)


@dataclass
class _Quota:
    """单次抓取的条数上限，跨多个 DOI、多个作者共用。"""

    limit: int
    used: int = 0
    truncated: bool = False

    @property
    def full(self) -> bool:
        return self.used >= self.limit

    def take(self, items: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        room = max(self.limit - self.used, 0)
        if len(items) > room:
            self.truncated = True
        self.used += min(len(items), room)
        return items[:room]


class SemanticScholarSource:
    capabilities: frozenset[type] = frozenset({KeywordSearch, CitationWatch, AuthorWatch})

    def __init__(
        self,
        name: str,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        today: Today = utc_today,
    ) -> None:
        self.name = name
        self._options = parse_options(SemanticScholarOptions, options, name)
        self._today = today
        default_interval = _INTERVAL_WITH_KEY if api_key else _INTERVAL_WITHOUT_KEY
        interval = self._options.min_interval_seconds
        self._http = HttpClient(
            "Semantic Scholar",
            base_url=self._options.base_url,
            contact_email=contact_email,
            secrets=[api_key],
            headers={"x-api-key": api_key} if api_key else None,
            min_interval=interval if "min_interval_seconds" in options else default_interval,
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
        quota = _Quota(options.max_records)
        self._http.reset_raw_log()
        if isinstance(query, KeywordSearch):
            self._keywords(query, window, builder, quota)
        elif isinstance(query, CitationWatch):
            self._citations(query, window, builder, quota)
        elif isinstance(query, AuthorWatch):
            self._authors(query, window, builder, quota)
        if quota.truncated:
            builder.truncate(options.max_records)
        return FetchResult(
            records=builder.records,
            next_cursor=next_cursor(window),
            warnings=builder.warnings,
            truncated=builder.truncated,
            raw=self._http.raw_log(),
        )

    def _keywords(
        self, query: KeywordSearch, window: Window, builder: RecordBuilder, quota: _Quota
    ) -> None:
        params = {
            "query": bulk_query(query),
            "fields": _FIELDS,
            "publicationDateOrYear": _date_range(window),
        }
        token: str | None = None
        while True:
            body = self._http.get_json("/paper/search/bulk", _with_token(params, token))
            items = _data(body)
            _add_all(builder, quota.take(items), window)
            token = body.get("token") or None
            if not items or not token:
                return
            if quota.full:
                quota.truncated = True
                return

    def _citations(
        self, query: CitationWatch, window: Window, builder: RecordBuilder, quota: _Quota
    ) -> None:
        for doi in sorted(query.dois):
            if quota.full:
                quota.truncated = True
                return
            path = f"/paper/{quote(f'DOI:{doi}', safe='/:')}/citations"
            pages = self._pages(path, window, quota)
            if not _consume(pages, builder, window, lambda item: item.get("citingPaper")):
                builder.note(f"Semantic Scholar 找不到 DOI {doi}，已跳过")

    def _authors(
        self, query: AuthorWatch, window: Window, builder: RecordBuilder, quota: _Quota
    ) -> None:
        for name in query.names:
            if quota.full:
                quota.truncated = True
                return
            author_id = self._author_id(name, builder)
            if author_id is None:
                continue
            path = f"/author/{quote(author_id, safe='')}/papers"
            _consume(self._pages(path, window, quota), builder, window, lambda item: item)

    def _author_id(self, name: str, builder: RecordBuilder) -> str | None:
        # 文档说明：带连字符的词查不到结果，要换成空格。
        params = {
            "query": name.replace("-", " "),
            "fields": "name,paperCount",
            "limit": str(self._options.author_candidates),
        }
        candidates = _data(self._http.get_json("/author/search", params))
        if not candidates:
            builder.note(f"找不到作者“{name}”，已跳过")
            return None
        first = candidates[0]
        same_name = [c for c in candidates if _fold(str(c.get("name", ""))) == _fold(name)]
        if same_name != [first] or not first.get("authorId"):
            builder.note(f"作者“{name}”有多个候选或姓名不完全一致，无法确定是谁，已跳过")
            return None
        return str(first["authorId"])

    def _pages(
        self, path: str, window: Window, quota: _Quota
    ) -> Iterator[list[Mapping[str, Any]]] | None:
        """按 offset 翻页。路径对应的论文或作者不存在时返回 None。"""
        params = {
            "fields": _FIELDS,
            "publicationDateOrYear": _date_range(window),
            "limit": str(self._options.page_size),
        }
        body = self._http.get_json(path, {**params, "offset": "0"}, allow_missing=True)
        if body is None:
            return None
        return self._more_pages(path, params, body, quota)

    def _more_pages(
        self, path: str, params: Mapping[str, str], body: Any, quota: _Quota
    ) -> Iterator[list[Mapping[str, Any]]]:
        offset = 0
        while True:
            items = _data(body)
            yield quota.take(items)
            following = body.get("next")
            if not items or not isinstance(following, int) or following <= offset:
                return
            if quota.full:
                quota.truncated = True
                return
            offset = following
            body = self._http.get_json(path, {**params, "offset": str(offset)})


def bulk_query(query: KeywordSearch) -> str:
    """("a" | "b") -("c" | "d")。排除符号必须紧贴括号，中间有空格会被当成普通词。"""
    included = boolean_query(KeywordSearch(terms=query.terms), or_op="|")
    if not query.exclude:
        return included
    excluded = boolean_query(KeywordSearch(terms=query.exclude), or_op="|")
    return f"{included} -{excluded}"


def _date_range(window: Window) -> str:
    return f"{window.since.isoformat()}:{window.until.isoformat()}"


def _with_token(params: Mapping[str, str], token: str | None) -> dict[str, str]:
    return {**params, "token": token} if token else dict(params)


def _data(body: Any) -> list[Mapping[str, Any]]:
    items = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(items, list):
        raise SchemaChanged("Semantic Scholar 返回缺少 data 列表")
    return [item for item in items if isinstance(item, Mapping)]


def _consume(
    pages: Iterator[list[Mapping[str, Any]]] | None,
    builder: RecordBuilder,
    window: Window,
    unwrap: Callable[[Mapping[str, Any]], Any],
) -> bool:
    if pages is None:
        return False
    for page in pages:
        papers = [unwrap(item) for item in page]
        _add_all(builder, [p for p in papers if isinstance(p, Mapping)], window)
    return True


def _fold(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).casefold().replace("-", " ")
    return " ".join("".join(ch if ch.isalnum() else " " for ch in normalized).split())


def _published(paper: Mapping[str, Any]) -> Any:
    found = parse_date(paper.get("publicationDate"))
    year = paper.get("year")
    if found is None and isinstance(year, int):
        return parse_date(str(year))
    return found


def _pmcid(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    return text if text.upper().startswith("PMC") else f"PMC{text}"


def _add_all(builder: RecordBuilder, papers: list[Mapping[str, Any]], window: Window) -> None:
    for paper in papers:
        published = _published(paper)
        if published is not None and window.since <= published <= window.until:
            _add(builder, paper, published)


def _add(builder: RecordBuilder, paper: Mapping[str, Any], published: Any) -> None:
    ids = paper.get("externalIds")
    ids = ids if isinstance(ids, Mapping) else {}
    journal = paper.get("journal")
    journal_name = journal.get("name") if isinstance(journal, Mapping) else None
    authors = [a.get("name") for a in paper.get("authors") or [] if isinstance(a, Mapping)]
    builder.add(
        title=paper.get("title") or "",
        abstract=strip_markup(paper.get("abstract")),
        authors=tuple(str(a) for a in authors if a),
        venue=paper.get("venue") or journal_name or None,
        published_at=published,
        doi=ids.get("DOI"),
        arxiv_id=ids.get("ArXiv"),
        external_ids={
            "s2": paper.get("paperId"),
            "pmid": ids.get("PubMed"),
            "pmcid": _pmcid(ids.get("PubMedCentral")),
        },
    )
