"""PubMed（NCBI E-utilities）：按关键词、期刊 ISSN、作者追踪新收录的生物医学文献。

先用 esearch 拿到 PMID 列表，再用 efetch 分批取 XML 详情。窗口用 Entrez 入库日期（edat），
晚入库的老文章也能接住；重叠窗口带来的重复由去重阶段处理。
NCBI 要求带 tool 和 email；有 API key 时每秒可请求 10 次，否则 3 次。
文档：https://www.ncbi.nlm.nih.gov/books/NBK25499/
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from datetime import date
from typing import Any

import httpx
from pydantic import Field

from litfactory.adapters.common.http import HttpClient
from litfactory.adapters.common.options import SourceOptions, Today, parse_options, utc_today
from litfactory.adapters.common.query import boolean_query
from litfactory.adapters.common.records import RecordBuilder, ensure_supported
from litfactory.adapters.common.text import parse_date
from litfactory.adapters.common.window import Window, next_cursor, window_from_cursor
from litfactory.domain.errors import SchemaChanged, TransientError
from litfactory.domain.query import AuthorWatch, JournalWatch, KeywordSearch, Query
from litfactory.ports.source import FetchResult

INTERVAL_WITH_KEY = 0.1
INTERVAL_WITHOUT_KEY = 0.34
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MEDLINE_DATE = re.compile(r"^(\d{4})(?:\s+([A-Za-z]{3})[a-z]*(?:\s+(\d{1,2}))?)?")
_SPACE = re.compile(r"\s+")


class PubMedOptions(SourceOptions):
    base_url: str = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    fetch_batch: int = Field(default=200, gt=0, le=200)


class PubMedSource:
    capabilities: frozenset[type] = frozenset({KeywordSearch, JournalWatch, AuthorWatch})

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
        self._options = parse_options(PubMedOptions, options, name)
        self._today = today
        self._common = {"tool": "litfactory"}
        if contact_email:
            self._common["email"] = contact_email
        if api_key:
            self._common["api_key"] = api_key
        default = INTERVAL_WITH_KEY if api_key else INTERVAL_WITHOUT_KEY
        explicit = "min_interval_seconds" in self._options.model_fields_set
        self.min_interval = self._options.min_interval_seconds if explicit else default
        self._http = HttpClient(
            "PubMed",
            base_url=self._options.base_url,
            contact_email=contact_email,
            secrets=(api_key,),
            min_interval=self.min_interval,
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
        ids = self._search(_term(query), window, builder)
        for start in range(0, len(ids), options.fetch_batch):
            for article in self._fetch_batch(ids[start : start + options.fetch_batch]):
                _add(builder, article)
        return FetchResult(
            records=builder.records,
            next_cursor=next_cursor(window),
            warnings=builder.warnings,
            truncated=builder.truncated,
            raw=self._http.raw_log(),
        )

    def _search(self, term: str, window: Window, builder: RecordBuilder) -> list[str]:
        limit = self._options.max_records
        params = {
            **self._common,
            "db": "pubmed",
            "term": term,
            "datetype": "edat",
            "mindate": window.since.strftime("%Y/%m/%d"),
            "maxdate": window.until.strftime("%Y/%m/%d"),
            "retmode": "json",
            "retmax": str(limit),
        }
        body = self._http.get_json("/esearch.fcgi", params)
        result = body.get("esearchresult") if isinstance(body, Mapping) else None
        error = _error_text(body, result)
        if error:
            raise TransientError(f"PubMed 检索暂时不可用：{self._http.scrub(error)[:400]}")
        ids = result.get("idlist") if isinstance(result, Mapping) else None
        if not isinstance(ids, list):
            raise SchemaChanged("PubMed esearch 返回缺少 esearchresult.idlist")
        total = _int(result.get("count")) if isinstance(result, Mapping) else 0
        if total > limit:
            builder.truncate(limit)
        return [str(item) for item in ids][:limit]

    def _fetch_batch(self, ids: list[str]) -> list[ET.Element]:
        params = {**self._common, "db": "pubmed", "id": ",".join(ids), "retmode": "xml"}
        response = self._http.get("/efetch.fcgi", params)
        content = response.content if response is not None else b""
        try:
            root = ET.fromstring(content)
        except ET.ParseError as error:
            raise SchemaChanged("PubMed efetch 返回的不是有效 XML") from error
        if root.tag != "PubmedArticleSet":
            raise SchemaChanged(f"PubMed efetch 返回了意外的根节点 {root.tag}")
        return root.findall("PubmedArticle")


def _quoted(term: str) -> str:
    return '"' + term.replace('"', " ").strip() + '"'


def _term(query: Query) -> str:
    if isinstance(query, KeywordSearch):
        return boolean_query(query, phrase=lambda term: f"{_quoted(term)}[tiab]")
    if isinstance(query, JournalWatch):
        return "(" + " OR ".join(f"{_quoted(i)}[is]" for i in sorted(query.issns)) + ")"
    if isinstance(query, AuthorWatch):
        return "(" + " OR ".join(f"{_quoted(n)}[au]" for n in query.names) + ")"
    raise ValueError(f"不支持的查询：{type(query).__name__}")


def _error_text(body: Any, result: Any) -> str | None:
    """NCBI 后端故障时有时仍返回 200，只在 JSON 里带 ERROR 字段。"""
    for holder in (result, body):
        if isinstance(holder, Mapping):
            for key in ("ERROR", "error"):
                if holder.get(key):
                    return str(holder[key])
    return None


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _text(node: ET.Element | None) -> str | None:
    """取节点内全部文字（含 <i>、<sub> 等内嵌标签），压缩空白。"""
    if node is None:
        return None
    text = _SPACE.sub(" ", "".join(node.itertext())).strip()
    return text or None


def _abstract(article: ET.Element) -> str | None:
    parts = []
    for node in article.findall("Abstract/AbstractText"):
        text = _text(node)
        if text:
            label = node.get("Label")
            parts.append(f"{label}: {text}" if label else text)
    return " ".join(parts) or None


def _author(node: ET.Element) -> str | None:
    collective = _text(node.find("CollectiveName"))
    if collective:
        return collective
    given = node.findtext("ForeName") or node.findtext("Initials")
    names = [part.strip() for part in (given, node.findtext("LastName")) if part and part.strip()]
    return " ".join(names) or None


def _month(value: str | None) -> int | None:
    if not value:
        return None
    if _is_number(value.strip()):
        return int(value)
    key = value.strip()[:3].lower()
    return _MONTHS.index(key) + 1 if key in _MONTHS else None


def _ymd(node: ET.Element | None) -> date | None:
    year = node.findtext("Year") if node is not None else None
    if node is None or not year or not _is_number(year.strip()):
        return None
    parts = [int(year)]
    month = _month(node.findtext("Month"))
    day = node.findtext("Day")
    if month:
        parts.append(month)
        if day and _is_number(day.strip()):
            parts.append(int(day))
    return parse_date(parts)


def _medline_date(value: str) -> date | None:
    """MedlineDate 是自由文本，例如 “2026 Nov-Dec”“1985 Mar 13-19”“2025 Winter”。"""
    match = _MEDLINE_DATE.match(value.strip())
    if not match:
        return None
    year, month_name, day = match.groups()
    month = _month(month_name)
    parts = [int(year), *([month] if month else []), *([int(day)] if month and day else [])]
    return parse_date(parts)


def _date(article: ET.Element) -> date | None:
    """电子出版日期（ArticleDate）优先，没有时用期刊出版日期。"""
    found = _ymd(article.find("ArticleDate"))
    if found:
        return found
    pub = article.find("Journal/JournalIssue/PubDate")
    medline = pub.findtext("MedlineDate") if pub is not None else None
    return _medline_date(medline) if medline else _ymd(pub)


def _ids(entry: ET.Element, article: ET.Element) -> dict[str, str]:
    ids = {
        node.get("IdType", ""): (node.text or "").strip()
        for node in entry.findall("PubmedData/ArticleIdList/ArticleId")
    }
    if not ids.get("doi"):
        for node in article.findall("ELocationID"):
            if node.get("EIdType") == "doi" and node.text:
                ids["doi"] = node.text.strip()
    return ids


def _add(builder: RecordBuilder, entry: ET.Element) -> None:
    citation = entry.find("MedlineCitation")
    article = citation.find("Article") if citation is not None else None
    if citation is None or article is None:
        builder.add(title="")
        return
    ids = _ids(entry, article)
    authors = (_author(node) for node in article.findall("AuthorList/Author"))
    builder.add(
        title=_text(article.find("ArticleTitle")) or "",
        abstract=_abstract(article),
        authors=tuple(a for a in authors if a),
        venue=_text(article.find("Journal/Title")),
        issn=_text(article.find("Journal/ISSN")),
        published_at=_date(article),
        doi=ids.get("doi") or None,
        external_ids={"pmid": _text(citation.find("PMID")), "pmcid": ids.get("pmc")},
    )


def _is_number(text: str) -> bool:
    """只认 ASCII 数字。str.isdigit() 对“²”之类也返回 True，随后 int() 会抛错。"""
    return text.isascii() and text.isdecimal()
