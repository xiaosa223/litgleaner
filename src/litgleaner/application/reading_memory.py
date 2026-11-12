"""打开与回看共享的只读摘要：只显示当前可核验关系和规范用户正文。"""

from collections.abc import Mapping

from litgleaner.application.context import AppContext
from litgleaner.application.memory import paper_views
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.application.stale_relations import _relation
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.memory import MemoryWarning
from litgleaner.domain.memory_citations import citation_index
from litgleaner.domain.paper_relation import PaperRelation, RelateBatch
from litgleaner.domain.reading_memory import (
    ReadingMemory,
    citation_links,
    note_excerpts,
    relation_cards,
)
from litgleaner.domain.stale import StaleItem, item
from litgleaner.ports.reading_memory import ReadingMemorySnapshot
from litgleaner.ports.stale import StaleSnapshot


def _basis(ctx: AppContext, data: ReadingMemorySnapshot) -> StaleInputs:
    return StaleInputs(
        ctx,
        StaleSnapshot(
            papers=data.facts.papers,
            fulltexts=data.facts.fulltexts,
            aliases=data.facts.aliases,
            reading=data.facts.reading,
            extractions=data.facts.extractions,
            assessments=(),
            scores=(),
            translations=(),
            questions=(),
            answers=(),
            ask_jobs={},
            topic_states=(),
            triages=(),
            maps=(),
            outlines=(),
            relations=data.relations,
            relate_batches=data.batches,
            library_paper_ids=frozenset(p.id for p in data.facts.papers),
            user_decided_relation_ids=data.user_decided_relation_ids,
            annotations=data.annotations,
        ),
    )


def _candidates(data: ReadingMemorySnapshot, paper_id: str) -> tuple[PaperRelation, ...]:
    current = sorted(
        (r for r in data.relations if r.status == "valid" and paper_id in (r.paper_a, r.paper_b)),
        key=lambda r: (r.created_at, r.id),
        reverse=True,
    )
    current.sort(
        key=lambda r: (
            not (r.source == "user" or r.id in data.user_decided_relation_ids),
            not (r.kind == "extends" and r.paper_b == paper_id),
        )
    )
    return tuple(current)


def _checked_relations(
    ctx: AppContext, data: ReadingMemorySnapshot, paper_id: str
) -> tuple[tuple[PaperRelation, ...], int, int, tuple[MemoryWarning, ...]]:
    basis, selected, warnings = _basis(ctx, data), [], []
    batches = {batch.id: batch for batch in data.batches}
    candidates = _candidates(data, paper_id)
    checked = 0
    for relation in candidates[:64]:
        checked += 1
        peer = relation.paper_b if relation.paper_a == paper_id else relation.paper_a
        if peer not in basis.snapshot.library_paper_ids:
            warnings.append(MemoryWarning(peer, "relation_outside_library", "关系对端已退出文库"))
            continue
        state = _checked_state(basis, relation, batches)
        if state.status != "current":
            warnings.append(
                MemoryWarning(
                    peer, f"relation_{state.status}", "；".join((*state.reasons, *state.unchecked))
                )
            )
            continue
        selected.append(relation)
        if len(selected) == 3:
            break
    if checked < len(candidates):
        warnings.append(
            MemoryWarning(paper_id, "relations_omitted", "关系只核验前64个候选，最多展示3条")
        )
    return tuple(selected), len(candidates), checked, tuple(warnings)


def _checked_state(
    basis: StaleInputs, relation: PaperRelation, batches: Mapping[str, RelateBatch]
) -> StaleItem:
    state = _relation(basis, relation, batches)
    batch = batches.get(relation.batch_id)
    if (
        batch is None
        or relation.source == "user"
        or relation.id in basis.snapshot.user_decided_relation_ids
    ):
        return state
    unchecked = list(state.unchecked)
    for paper_id in batch.input_paper_ids:
        try:
            basis.document(paper_id)
        except ValueError:
            unchecked.append("核实批次的已发送原文无法完整读取")
    return item(
        state.kind,
        state.id,
        state.paper_id,
        state.topic_id,
        reasons=state.reasons,
        unchecked=tuple(unchecked),
        selected=state.selected,
    )


def view(ctx: AppContext, paper_id: str, *, limit: int = 50, offset: int = 0) -> ReadingMemory:
    if type(limit) is not int or not 1 <= limit <= 200 or type(offset) is not int or offset < 0:
        raise InvalidRequest("阅读记忆查询参数越界")
    data = ctx.store.reading_memory_snapshot(paper_id)
    papers = paper_views(data.facts)
    if paper_id not in papers:
        raise NotFound(f"论文尚未进入文库或不存在：{paper_id}")
    notes = note_excerpts(data.annotations)
    index = citation_index(
        data.facts.citations, data.facts.aliases, {p.id: p.title for p in data.facts.papers}
    )
    links = citation_links(paper_id, papers, index, notes)
    relations, candidates, checked, warnings = _checked_relations(ctx, data, paper_id)
    return ReadingMemory(
        papers[paper_id],
        relation_cards(paper_id, papers, relations, notes),
        links[offset : offset + limit],
        len(links),
        limit,
        offset,
        candidates,
        checked,
        candidates > checked,
        warnings,
    )
