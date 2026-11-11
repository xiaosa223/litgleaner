"""知识库事实查询：不生成内容，不推断掌握程度，也不重算旧提取。"""

from dataclasses import dataclass, replace
from typing import Literal

from litgleaner.application.context import AppContext
from litgleaner.application.stages.extract import (
    extraction_input_hash,
    extraction_producer_version,
)
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.knowledge import Concept, ConceptAlias, ConceptOccurrence
from litgleaner.domain.reading import BBox


@dataclass(frozen=True)
class OccurrenceView:
    occurrence: ConceptOccurrence
    paper_title: str
    stale: bool
    definition_authorship: Literal["author"] | None


@dataclass(frozen=True)
class Definition:
    quote: str
    paper_id: str
    page: int
    bbox: BBox | None
    stale: bool
    authorship: Literal["author"] = "author"


@dataclass(frozen=True)
class ConceptSummary:
    concept: Concept
    aliases: tuple[ConceptAlias, ...]
    papers: int
    definition: Definition | None


@dataclass(frozen=True)
class KnowledgePage:
    items: tuple[ConceptSummary, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True)
class ConceptDetail:
    concept: Concept
    aliases: tuple[ConceptAlias, ...]
    occurrences: tuple[OccurrenceView, ...]


@dataclass(frozen=True)
class PaperConcept:
    concept: Concept
    occurrence: OccurrenceView


def _stale(ctx: AppContext, item: ConceptOccurrence) -> bool:
    state = ctx.store.papers.get_fulltext(item.paper_id)
    if state is None or state.doc_key is None:
        return True
    expected = extraction_input_hash(
        state.doc_key,
        ctx.settings.chunker,
        ctx.settings.reading_language,
        ctx.settings.assess_token_budget,
    )
    producer = extraction_producer_version(ctx)
    return item.input_hash != expected or (
        producer is not None and item.producer_version != producer
    )


def _occurrence(ctx: AppContext, item: ConceptOccurrence) -> OccurrenceView:
    paper = ctx.store.papers.get(item.paper_id)
    # 防御旧记录：事实接口只把核验过的定义暴露为原文。
    item = replace(item, definition=item.definition if item.verified else None)
    return OccurrenceView(
        item,
        paper.title if paper else item.paper_id,
        _stale(ctx, item),
        "author" if item.verified and item.definition else None,
    )


def _summary(ctx: AppContext, item: Concept) -> ConceptSummary:
    occurrences = ctx.store.knowledge.occurrences(item.id)
    candidates = [o for o in occurrences if o.verified and o.definition]
    candidates.sort(key=lambda o: (_stale(ctx, o), o.created_at, o.paper_id))
    chosen = candidates[0] if candidates else None
    definition = (
        Definition(
            chosen.definition or "",
            chosen.paper_id,
            chosen.page,
            chosen.bbox,
            _stale(ctx, chosen),
        )
        if chosen
        else None
    )
    return ConceptSummary(item, ctx.store.knowledge.aliases(item.id), len(occurrences), definition)


def concepts(
    ctx: AppContext,
    *,
    topic_id: str | None = None,
    query: str = "",
    limit: int = 50,
    offset: int = 0,
) -> KnowledgePage:
    if not 1 <= limit <= 200 or offset < 0 or len(query) > 500:
        raise InvalidRequest("知识库查询参数越界")
    items = ctx.store.knowledge.list(topic_id, query, limit, offset)
    return KnowledgePage(
        tuple(_summary(ctx, item) for item in items),
        ctx.store.knowledge.count(topic_id, query),
        limit,
        offset,
    )


def concept(ctx: AppContext, concept_id: str) -> ConceptDetail:
    item = ctx.store.knowledge.get(concept_id)
    if item is None:
        raise NotFound("概念不存在")
    return ConceptDetail(
        item,
        ctx.store.knowledge.aliases(item.id),
        tuple(_occurrence(ctx, o) for o in ctx.store.knowledge.occurrences(item.id)),
    )


def paper_concepts(ctx: AppContext, paper_id: str) -> tuple[PaperConcept, ...]:
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound("论文不存在")
    result: list[PaperConcept] = []
    for occurrence in ctx.store.knowledge.for_paper(paper_id):
        item = ctx.store.knowledge.get(occurrence.concept_id)
        if item is not None:
            result.append(PaperConcept(item, _occurrence(ctx, occurrence)))
    return tuple(result)
