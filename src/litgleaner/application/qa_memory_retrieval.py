"""问答的规范记忆召回：FTS、当前关系和已就绪本地向量共用有界来源池。"""

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any, Literal

from litgleaner.application.context import AppContext
from litgleaner.application.memory import _input_problem
from litgleaner.application.qa_memory_candidates import (
    Candidate,
    ScanBudget,
    author_source,
    balanced_candidates,
    document_candidates,
    lexical_score,
)
from litgleaner.application.qa_memory_vectors import prepare, rerank
from litgleaner.application.stages.extract import extraction_producer_version
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.application.stale_relations import _relation
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper_relation import PaperRelation
from litgleaner.domain.qa import Question
from litgleaner.domain.qa_memory import QASource
from litgleaner.ports.memory import MemorySnapshot
from litgleaner.ports.search import QASearchHit


@dataclass(frozen=True)
class QARetrieval:
    sources: tuple[QASource, ...]
    relations: tuple[PaperRelation, ...]
    backgrounds: Mapping[str, Mapping[str, Any]]
    warnings: tuple[str, ...]


def _merge(candidates: dict[str, Candidate], items: tuple[Candidate, ...]) -> None:
    for item in items:
        previous = candidates.get(item.source.id)
        candidates[item.source.id] = (
            item
            if previous is None
            else Candidate(
                item.source, max(previous.lexical, item.lexical), max(previous.fts, item.fts)
            )
        )


def _relations(
    basis: StaleInputs, question: Question, budget: ScanBudget
) -> tuple[tuple[PaperRelation, ...], tuple[QASource, ...], tuple[str, ...]]:
    batches = {row.id: row for row in basis.snapshot.relate_batches}
    candidates = sorted(
        (
            row
            for row in basis.snapshot.relations
            if row.status == "valid" and question.paper_id in (row.paper_a, row.paper_b)
        ),
        key=lambda row: (row.source != "user", row.created_at, row.id),
    )
    chosen: list[PaperRelation] = []
    sources: list[QASource] = []
    warnings = ["关系候选超过64条，本次只核对前64条"] if len(candidates) > 64 else []
    for row in candidates[:64]:
        if len(chosen) >= 3 or budget.sources < 2:
            break
        if _relation(basis, row, batches).status != "current":
            warnings.append(f"{row.id}：关系已过期或无法完整核验，未用于问答")
            continue
        evidence = (row.evidence_a, row.evidence_b)
        if sum(len(item.quote) for item in evidence) > budget.chars:
            warnings.append(f"{row.id}：关系原文超过候选扫描配额，未用于问答")
            continue
        for item in evidence:
            accepted = budget.whole(item.quote)
            assert accepted
            sources.append(
                QASource(
                    f"relation:{row.id}:{item.paper_id}",
                    item.paper_id,
                    "author",
                    item.quote,
                    item.page,
                    item.doc_key,
                )
            )
        chosen.append(row)
    return tuple(chosen), tuple(sources), tuple(warnings)


def _user_source(
    hit: QASearchHit,
    basis: StaleInputs,
    annotations: Mapping[str, Any],
    questions: Mapping[str, Question],
) -> QASource | None:
    kind: Literal["note", "question"]
    if hit.authorship != "user":
        return None
    if hit.kind in {"note", "highlight"}:
        id_, suffix = hit.id.rsplit(":", 1) if ":" in hit.id else ("", "")
        record = annotations.get(id_)
        if suffix != "body" or record is None or record.body != hit.text:
            return None
        kind, text = "note", record.body
    elif hit.kind == "qa":
        id_ = hit.id
        record = questions.get(id_)
        if record is None or record.text != hit.text:
            return None
        kind, text = "question", record.text
    else:
        return None
    if record.paper_id != hit.paper_id or not isinstance(text, str) or not text.strip():
        return None
    return QASource(
        "",
        record.paper_id,
        kind,
        text,
        record.page,
        source_id=id_,
        version=stable_hash(asdict(record)),
    )


def _author_hit(hit: QASearchHit, basis: StaleInputs) -> QASource | None:
    state = basis.snapshot.fulltexts.get(hit.paper_id)
    if (
        hit.kind != "fulltext"
        or hit.authorship != "author"
        or state is None
        or state.doc_key is None
        or hit.source_version != state.doc_key
        or not hit.id.isdecimal()
    ):
        return None
    try:
        blocks = basis.document(hit.paper_id).blocks
        index = int(hit.id)
        if index >= len(blocks):
            return None
        block = blocks[index]
    except ValueError:
        return None
    if block.page != hit.page or block.text != hit.text:
        return None
    return author_source(hit.paper_id, block.page, block.text, state.doc_key)


def _hit_candidates(
    hits: tuple[QASearchHit, ...], basis: StaleInputs, question: Question, budget: ScanBudget
) -> tuple[Candidate, ...]:
    annotations = {row.id: row for row in basis.snapshot.annotations}
    questions = {row.id: row for row in basis.snapshot.questions}
    accepted: list[Candidate] = []
    for hit in hits[:64]:
        if (
            hit.paper_id != question.paper_id
            and hit.paper_id not in basis.snapshot.library_paper_ids
        ):
            continue
        source = (
            _author_hit(hit, basis)
            if hit.authorship == "author"
            else _user_source(hit, basis, annotations, questions)
        )
        if source is None or not math.isfinite(hit.rank):
            continue
        for piece in budget.pieces(source.text, chars=100_000):
            id_ = f"{source.kind}:{source.paper_id}:{source.source_id}:{stable_hash(piece)[:16]}"
            fragment = (
                author_source(source.paper_id, source.page, piece, source.doc_key)
                if source.kind == "author"
                and source.page is not None
                and source.doc_key is not None
                else replace(source, id=id_, text=piece)
            )
            accepted.append(
                Candidate(fragment, lexical_score(piece, question), max(0.0, -hit.rank))
            )
    peak = max((row.fts for row in accepted), default=0.0)
    return tuple(replace(row, fts=row.fts / peak if peak else 0.0) for row in accepted)


def _backgrounds(
    ctx: AppContext, basis: StaleInputs, paper_ids: set[str]
) -> Mapping[str, Mapping[str, Any]]:
    producer = extraction_producer_version(ctx)
    facts = MemorySnapshot((), {}, (), basis.snapshot.fulltexts, (), (), (), {})
    result = {}
    for paper_id in sorted(paper_ids):
        extraction = basis.extractions.get(paper_id)
        if (
            extraction is None
            or not isinstance(extraction.data, Mapping)
            or producer is None
            or extraction.extractor_version != producer
            or _input_problem(ctx, facts, extraction) is not None
        ):
            continue
        fields = {}
        for name in ("summary", "problem", "approach"):
            raw = extraction.data.get(name)
            value = raw.get("text") if isinstance(raw, Mapping) else raw
            if isinstance(value, str) and value.strip():
                fields[name] = value[:200]
        if fields:
            result[paper_id] = {
                "id": extraction.id,
                "input_hash": extraction.input_hash,
                "producer_version": extraction.extractor_version,
                "content_hash": stable_hash(asdict(extraction)),
                "fields": fields,
            }
    return result


def collect(ctx: AppContext, question: Question, basis: StaleInputs) -> QARetrieval:
    budget = ScanBudget(2_000_000 - len(question.selection or ""), 4000 - bool(question.selection))
    warnings: list[str] = []
    candidates: dict[str, Candidate] = {}
    try:
        _merge(candidates, document_candidates(basis, question, question.paper_id, budget))
    except ValueError:
        warnings.append("本篇原文不可读，未加入正文候选")
    relations, relation_sources, relation_warnings = _relations(basis, question, budget)
    warnings.extend(relation_warnings)
    vectors, vector_warnings = prepare(ctx, question, basis)
    warnings.extend(vector_warnings)
    allowed = tuple(sorted(basis.snapshot.library_paper_ids | {question.paper_id}))
    hits = ctx.store.search.qa_hits(question.text, allowed, limit=64)
    _merge(candidates, _hit_candidates(hits, basis, question, budget))
    for paper_id in vectors.paper_ids if vectors is not None else ():
        try:
            _merge(candidates, document_candidates(basis, question, paper_id, budget))
        except ValueError:
            warnings.append(f"{paper_id}：相关原文不可读，未加入正文候选")
    ranked, rank_warnings = rerank(ctx, vectors, balanced_candidates(candidates, question.paper_id))
    sources = (*relation_sources, *(row.source for row in ranked))
    backgrounds = _backgrounds(ctx, basis, {row.paper_id for row in sources})
    return QARetrieval(
        sources, relations, backgrounds, tuple(dict.fromkeys((*warnings, *rank_warnings)))
    )
