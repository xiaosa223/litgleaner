"""OpenAlex：按期刊、关键词、被引种子和作者追踪新文章。

几条实测（2026-11-05）决定了这里的写法：
- 按入库日期筛选（from_created_date）需要付费套餐，所以窗口只能按出版日期。很多论文在出版
  几周后才入库：抽样显示约 12%–15% 晚于 30 天、1%–6% 晚于 60 天，因此默认重叠 60 天，
  重复的记录由去重阶段处理。
- 没有 API key 时 cursor 分页只能取第一页（第二页返回 401），所以无 key 时改用 page 分页
  （page × per_page 不超过 10000，max_records 的上限正好是 10000）。有 key 时用 cursor 分页。
- per_page 官方上限 100（200 是将被移除的旧行为）。检索请求比筛选贵十倍，匿名检索在服务端
  繁忙时会被限流，配置 OPENALEX_API_KEY 更稳定。key 放在 Authorization 头里，不进 URL。
- 关键词支持 AND/OR/NOT 和引号短语，检索范围是标题、摘要和 OpenAlex 关键词（不含全文，
  避免只在参考文献里提到的论文混进来）。
文档：https://help.openalex.org/api/
"""

import re
import unicodedata
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import Field

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.options import SourceOptions, Today, parse_options, utc_today
from litgleaner.adapters.common.query import boolean_query
from litgleaner.adapters.common.records import RecordBuilder, ensure_supported
from litgleaner.adapters.common.text import parse_date, strip_markup
from litgleaner.adapters.common.window import Window, next_cursor, window_from_cursor
from litgleaner.adapters.sources.historical_common import HistoryCollector, citation_count
from litgleaner.adapters.sources.openalex_history import OpenAlexHistory
from litgleaner.adapters.sources.openalex_references import OpenAlexReferences
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.identity import normalize_arxiv_id, normalize_doi
from litgleaner.domain.query import (
    AuthorWatch,
    CitationWatch,
    HistoricalSearch,
    JournalWatch,
    KeywordSearch,
    Query,
    ReferenceList,
)
from litgleaner.ports.source import FetchResult

_SELECT = (
    "id,doi,title,publication_date,authorships,abstract_inverted_index,"
    "primary_location,ids,locations,type,cited_by_count"
)
_AUTHOR_SELECT = "id,display_name,works_count"
_MAX_OR_VALUES = 100
_PAGE_LIMIT = 10_000


class OpenAlexOptions(SourceOptions):
    base_url: str = "https://api.openalex.org"
    overlap_days: int = Field(default=60, ge=0, le=365)
    per_page: int = Field(default=100, gt=0, le=100)
    author_candidates: int = Field(default=10, gt=0, le=50)
    min_interval_seconds: float = Field(default=0.2, ge=0)


class OpenAlexSource:
    capabilities: frozenset[type] = frozenset(
        {JournalWatch, KeywordSearch, CitationWatch, AuthorWatch, ReferenceList, HistoricalSearch}
    )

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
        self._options = parse_options(OpenAlexOptions, options, name)
        self._contact = contact_email
        self._keyed = bool(api_key)
        self._today = today
        self._http = HttpClient(
            "OpenAlex",
            base_url=self._options.base_url,
            contact_email=contact_email,
            secrets=(api_key,),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
            min_interval=self._options.min_interval_seconds,
            timeout=self._options.timeout_seconds,
            transport=transport,
        )

    def fetch(self, query: Query, cursor: str | None) -> FetchResult:
        ensure_supported(self.capabilities, query)
        options = self._options
        if isinstance(query, HistoricalSearch):
            return OpenAlexHistory(
                self._http,
                HistoryCollector(query, RecordBuilder(self.name), options.max_records, "review"),
                work_fields,
                self._polite({"select": _SELECT}),
                options.per_page,
                self._keyed,
            ).fetch(query)
        if isinstance(query, ReferenceList):
            return OpenAlexReferences(
                self._http,
                RecordBuilder(self.name),
                limit=options.max_records,
                batch_size=min(options.per_page, _MAX_OR_VALUES),
                params=self._polite({"select": _SELECT}),
                fields=work_fields,
            ).fetch(query)
        window = window_from_cursor(
            cursor,
            self._today(),
            lookback_days=options.lookback_days,
            overlap_days=options.overlap_days,
        )
        builder = RecordBuilder(self.name)
        self._http.reset_raw_log()
        seen = 0
        for params in self._listings(query, builder):
            seen = self._collect(params, window, builder, seen)
            if seen >= options.max_records:
                break
        return FetchResult(
            records=builder.records,
            next_cursor=next_cursor(window),
            warnings=builder.warnings,
            truncated=builder.truncated,
            raw=self._http.raw_log(),
        )

    def _listings(self, query: Query, builder: RecordBuilder) -> Iterator[dict[str, str]]:
        """把查询翻译成一组 /works 列表请求的参数（不含日期和分页）。"""
        if isinstance(query, KeywordSearch):
            yield {"search.title_abstract_keywords": boolean_query(query)}
            return
        if isinstance(query, JournalWatch):
            field, values = "primary_location.source.issn", sorted(set(query.issns))
        elif isinstance(query, CitationWatch):
            field, values = "cites", self._seed_ids(query.dois, builder)
        elif isinstance(query, AuthorWatch):
            field, values = "authorships.author.id", self._author_ids(query.names, builder)
        else:  # pragma: no cover - ensure_supported 已拦截
            return
        for start in range(0, len(values), _MAX_OR_VALUES):
            yield {"filter": f"{field}:{'|'.join(values[start : start + _MAX_OR_VALUES])}"}

    def _seed_ids(self, dois: tuple[str, ...], builder: RecordBuilder) -> list[str]:
        """种子 DOI 换成 OpenAlex 的 W 编号。单篇查询免费。"""
        ids: list[str] = []
        for doi in dois:
            body = self._http.get_json(
                f"/works/doi:{quote(doi.strip(), safe='/')}",
                self._polite({"select": "id"}),
                allow_missing=True,
            )
            work_id = short_id(body.get("id")) if isinstance(body, Mapping) else None
            if work_id is None:
                builder.note(f"种子 DOI {doi} 在 OpenAlex 中找不到，已跳过")
            elif work_id not in ids:
                ids.append(work_id)
        return ids

    def _author_ids(self, names: tuple[str, ...], builder: RecordBuilder) -> list[str]:
        """只有首个候选明显就是这个人时才采用；同名或对不上时告警跳过，不猜。"""
        ids: list[str] = []
        for name in names:
            params = {
                "search": name,
                "select": _AUTHOR_SELECT,
                "per_page": str(self._options.author_candidates),
            }
            body = self._http.get_json("/authors", self._polite(params))
            results = body.get("results") if isinstance(body, Mapping) else None
            if not isinstance(results, list):
                raise SchemaChanged("OpenAlex 作者检索返回缺少 results")
            author_id, problem = pick_author(name, [r for r in results if isinstance(r, Mapping)])
            if author_id is None:
                builder.note(f"作者“{name}”{problem}，已跳过")
            elif author_id not in ids:
                ids.append(author_id)
        return ids

    def _collect(
        self, base: Mapping[str, str], window: Window, builder: RecordBuilder, seen: int
    ) -> int:
        """按页读取一个列表请求，返回累计读到的条目数（按原始条目计，不按去重后的记录计）。"""
        options = self._options
        dates = (
            f"from_publication_date:{window.since.isoformat()},"
            f"to_publication_date:{window.until.isoformat()}"
        )
        joined = f"{base['filter']},{dates}" if "filter" in base else dates
        params = {
            **base,
            "filter": joined,
            "sort": "publication_date:desc",
            "per_page": str(options.per_page),
            "select": _SELECT,
        }
        page, page_cursor = 1, "*"
        while True:
            paging = {"cursor": page_cursor} if self._keyed else {"page": str(page)}
            items, more = self._page({**params, **paging}, page)
            for item in items:
                builder.add(**work_fields(item))
            seen += len(items)
            if seen >= options.max_records:
                builder.truncate(options.max_records)
                return seen
            if not items or not more:
                return seen
            page, page_cursor = page + 1, more

    def _page(self, params: Mapping[str, str], page: int) -> tuple[list[Mapping[str, Any]], str]:
        """返回本页条目和“还有下一页”的标记（cursor 模式下就是下一页的 cursor）。"""
        body = self._http.get_json("/works", self._polite(params))
        meta = body.get("meta") if isinstance(body, Mapping) else None
        results = body.get("results") if isinstance(body, Mapping) else None
        if not isinstance(meta, Mapping) or not isinstance(results, list):
            raise SchemaChanged("OpenAlex 返回缺少 meta 或 results")
        items = [item for item in results if isinstance(item, Mapping)]
        if self._keyed:
            return items, str(meta.get("next_cursor") or "")
        count = meta.get("count")
        reached = page * self._options.per_page
        full_page = len(items) >= self._options.per_page
        more = full_page and isinstance(count, int) and count > reached and reached < _PAGE_LIMIT
        return items, "more" if more else ""

    def _polite(self, params: Mapping[str, str]) -> dict[str, str]:
        return {**params, "mailto": self._contact} if self._contact else dict(params)


# ---- 条目映射 ----

_ARXIV_DOI = "10.48550/arxiv."
_ARXIV_OAI = "pmh:oai:arxiv.org:"
_MAX_ABSTRACT_WORDS = 20_000
# 同名候选的作品数达到首个候选的这个比例，就认为分不清是谁。
_NAMESAKE_RATIO = 0.2


def short_id(value: Any) -> str | None:
    """https://openalex.org/W123 → W123。"""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.rstrip("/").rsplit("/", 1)[-1] or None


def _id_tail(ids: Mapping[str, Any], key: str) -> str | None:
    value = ids.get(key)
    return short_id(value) if isinstance(value, str) else None


def _pmcid(ids: Mapping[str, Any]) -> str | None:
    """OpenAlex 给的是 .../pmc/articles/1817752，统一写成 PMC1817752。"""
    tail = _id_tail(ids, "pmcid")
    if not tail:
        return None
    digits = tail.upper().removeprefix("PMC")
    return f"PMC{digits}" if _is_number(digits) else None


def _abstract(index: Any) -> str | None:
    """摘要以倒排索引给出（词 → 位置列表），按位置还原。"""
    if not isinstance(index, Mapping):
        return None
    words: dict[int, str] = {}
    for word, positions in index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            continue
        for position in positions:
            if isinstance(position, int) and 0 <= position < _MAX_ABSTRACT_WORDS:
                words[position] = word
    return strip_markup(" ".join(words[p] for p in sorted(words)))


def _arxiv_id(item: Mapping[str, Any]) -> str | None:
    """只认 arXiv 自己的 DOI 和 OAI 编号；多个来源说法不一致时宁可不填。"""
    candidates: list[str] = []
    dois = [item.get("doi")]
    for location in item.get("locations") or []:
        if not isinstance(location, Mapping):
            continue
        dois.append(location.get("doi"))
        oai = str(location.get("id") or "")
        if oai.lower().startswith(_ARXIV_OAI):
            candidates.append(oai[len(_ARXIV_OAI) :])
    for doi in dois:
        normalized = normalize_doi(doi if isinstance(doi, str) else None)
        if normalized and normalized.startswith(_ARXIV_DOI):
            candidates.append(normalized[len(_ARXIV_DOI) :])
    found = {arxiv for arxiv in map(normalize_arxiv_id, candidates) if arxiv}
    return found.pop() if len(found) == 1 else None


def _authors(item: Mapping[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for authorship in item.get("authorships") or []:
        if not isinstance(authorship, Mapping):
            continue
        author = authorship.get("author")
        name = author.get("display_name") if isinstance(author, Mapping) else None
        name = name or authorship.get("raw_author_name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return tuple(names)


def _venue(item: Mapping[str, Any]) -> tuple[str | None, str | None]:
    location = item.get("primary_location")
    source = location.get("source") if isinstance(location, Mapping) else None
    if not isinstance(source, Mapping):
        return None, None
    issns = source.get("issn")
    issn = source.get("issn_l") or (issns[0] if isinstance(issns, list) and issns else None)
    name = source.get("display_name")
    return (name if isinstance(name, str) else None), (issn if isinstance(issn, str) else None)


def work_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    """一条 OpenAlex work 变成 RecordBuilder.add 的参数。"""
    raw_ids = item.get("ids")
    ids = raw_ids if isinstance(raw_ids, Mapping) else {}
    title = item.get("title") or item.get("display_name")
    venue, issn = _venue(item)
    publication_date = item.get("publication_date")
    raw_doi = item.get("doi")
    return {
        "title": strip_markup(title if isinstance(title, str) else None) or "",
        "abstract": _abstract(item.get("abstract_inverted_index")),
        "authors": _authors(item),
        "venue": venue,
        "issn": issn,
        "published_at": parse_date(publication_date if isinstance(publication_date, str) else None),
        "doi": normalize_doi(raw_doi if isinstance(raw_doi, str) else None),
        "arxiv_id": _arxiv_id(item),
        "publication_types": (item["type"],)
        if isinstance(item.get("type"), str) and item["type"].strip()
        else (),
        "citation_count": citation_count(item.get("cited_by_count")),
        "external_ids": {
            "openalex": short_id(item.get("id")),
            "pmid": _id_tail(ids, "pmid"),
            "pmcid": _pmcid(ids),
        },
    }


def _name_key(name: str) -> frozenset[str]:
    """忽略大小写、重音、标点、词序和单字母缩写：Geoffrey E. Hinton 与 Geoffrey Hinton 相同。"""
    decomposed = unicodedata.normalize("NFKD", name)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    return frozenset(token for token in re.findall(r"\w+", plain) if len(token) > 1)


def _works(candidate: Mapping[str, Any]) -> int:
    count = candidate.get("works_count")
    return count if isinstance(count, int) else 0


def pick_author(name: str, candidates: list[Mapping[str, Any]]) -> tuple[str | None, str]:
    """首个候选的显示名与查询一致，且没有作品数相当的同名候选时才采用。返回 (编号, 原因)。"""
    key = _name_key(name)
    if not key or not candidates:
        return None, "在 OpenAlex 中找不到"
    first, *others = candidates
    if _name_key(str(first.get("display_name") or "")) != key:
        return None, "的首个检索结果不是同名作者，无法确定是谁"
    threshold = _works(first) * _NAMESAKE_RATIO
    for other in others:
        same_name = _name_key(str(other.get("display_name") or "")) == key
        if same_name and _works(other) >= threshold:
            return None, "有多个同名作者档案，存在歧义"
    author_id = short_id(first.get("id"))
    return (author_id, "") if author_id else (None, "的检索结果缺少编号")


def _is_number(text: str) -> bool:
    """只认 ASCII 数字。str.isdigit() 对“²”之类也返回 True，随后 int() 会抛错。"""
    return text.isascii() and text.isdecimal()
