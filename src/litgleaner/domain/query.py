"""信号源查询。适配器只认识查询，不认识主题。"""

from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.hashing import stable_hash
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


class CitationWatch(_QueryBase):
    kind: Literal["citation_watch"] = "citation_watch"
    dois: tuple[str, ...]


class AuthorWatch(_QueryBase):
    kind: Literal["author_watch"] = "author_watch"
    names: tuple[str, ...]


class InboxDrain(_QueryBase):
    kind: Literal["inbox_drain"] = "inbox_drain"


Query = JournalWatch | KeywordSearch | CitationWatch | AuthorWatch | InboxDrain

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
