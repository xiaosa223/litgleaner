"""信号源查询。适配器只认识查询，不认识主题。"""

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Annotated, Any, Literal, Self

from pydantic import Field, StringConstraints, TypeAdapter, model_validator

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.identity import normalize_doi
from litgleaner.domain.topic import Topic


class _QueryBase(FrozenModel):
    @property
    def hash(self) -> str:
        data = self.model_dump(mode="json")
        return stable_hash({k: sorted(v) if isinstance(v, list) else v for k, v in data.items()})


class JournalWatch(_QueryBase):
    kind: Literal["journal_watch"] = "journal_watch"
    issns: tuple[str, ...]


class KeywordSearch(_QueryBase):
    kind: Literal["keyword_search"] = "keyword_search"
    terms: tuple[str, ...]
    exclude: tuple[str, ...] = ()


_HistoricalTerm = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
]


class HistoricalSearch(_QueryBase):
    """用户显式发起的有界历史查询，不随日常发现计划运行。"""

    kind: Literal["historical_search"] = "historical_search"
    terms: tuple[_HistoricalTerm, ...] = Field(min_length=1, max_length=100)
    exclude: tuple[_HistoricalTerm, ...] = Field(default=(), max_length=100)
    since: date
    until: date
    category: Literal["reviews", "highly_cited", "recent"]
    limit: int = Field(default=100, strict=True, ge=1, le=1000)

    @model_validator(mode="after")
    def _date_order(self) -> Self:
        if self.since > self.until:
            raise ValueError("历史查询开始日期不能晚于结束日期")
        return self


class CitationWatch(_QueryBase):
    kind: Literal["citation_watch"] = "citation_watch"
    dois: tuple[str, ...]


class HistoricalCitations(_QueryBase):
    """显式日期范围内的被引扩展，与日常跟踪使用不同查询指纹。"""

    kind: Literal["historical_citations"] = "historical_citations"
    dois: tuple[str, ...] = Field(min_length=1, max_length=50)
    since: date
    until: date
    limit: int = Field(default=100, strict=True, ge=1, le=1000)

    @model_validator(mode="after")
    def _bounds(self) -> Self:
        if self.since > self.until:
            raise ValueError("历史被引查询开始日期不能晚于结束日期")
        normalized = tuple(normalize_doi(value) for value in self.dois)
        if any(value is None for value in normalized):
            raise ValueError("历史被引查询需要有效 DOI")
        return self


class ReferenceList(_QueryBase):
    """一次性读取种子论文引用的文献，不进入日常发现计划。"""

    kind: Literal["reference_list"] = "reference_list"
    dois: tuple[str, ...]


class AuthorWatch(_QueryBase):
    kind: Literal["author_watch"] = "author_watch"
    names: tuple[str, ...]


class InboxDrain(_QueryBase):
    kind: Literal["inbox_drain"] = "inbox_drain"


class IdentifierLookup(_QueryBase):
    """按标识符查一篇论文的元数据（用户添加论文，第 12.8 节）。一次性查询，不用游标。"""

    kind: Literal["identifier_lookup"] = "identifier_lookup"
    doi: str | None = None
    arxiv_id: str | None = None


Query = (
    JournalWatch
    | KeywordSearch
    | HistoricalSearch
    | CitationWatch
    | HistoricalCitations
    | ReferenceList
    | AuthorWatch
    | InboxDrain
    | IdentifierLookup
)

_ADAPTER: TypeAdapter[Query] = TypeAdapter(Annotated[Query, Field(discriminator="kind")])


def query_from_payload(payload: Mapping[str, Any]) -> Query:
    return _ADAPTER.validate_python(_ADAPTER.dump_python(_ADAPTER.validate_python(dict(payload))))


def _topic_queries(topic: Topic, journal_sets: Mapping[str, tuple[str, ...]]) -> Iterable[Query]:
    issns = tuple(issn for name in topic.journal_sets for issn in journal_sets.get(name, ()))
    if issns:
        yield JournalWatch(issns=tuple(sorted(set(issns))))
    if topic.query.terms:
        yield KeywordSearch(terms=topic.query.terms, exclude=topic.query.exclude)
    if topic.seeds.dois:
        yield CitationWatch(dois=topic.seeds.dois)
    if topic.seeds.authors:
        yield AuthorWatch(names=topic.seeds.authors)


def plan_queries(
    topics: Iterable[Topic], journal_sets: Mapping[str, tuple[str, ...]]
) -> tuple[Query, ...]:
    """多个主题要查同样的内容时只查一次。收件箱查询总是存在。"""
    unique: dict[str, Query] = {}
    for topic in topics:
        for query in _topic_queries(topic, journal_sets):
            unique.setdefault(query.hash, query)
    inbox = InboxDrain()
    unique.setdefault(inbox.hash, inbox)
    return tuple(unique.values())
