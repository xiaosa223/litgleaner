"""合并事实、向量与已有文本的只读候选，不补索引或执行语义核实。"""

from collections.abc import Mapping
from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.embeddings import VectorProjection, project_vectors
from litgleaner.application.memory import fact_signals, paper_views
from litgleaner.application.memory_candidate_text import text_signals
from litgleaner.domain.embedding import cosine
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.knowledge import normalize_concept_name
from litgleaner.domain.memory import MemoryPaper, MemoryWarning, PaperFacts
from litgleaner.domain.memory_candidate_views import (
    MemoryCandidate,
    MemoryCandidates,
    TextEvidence,
    VectorEvidence,
)
from litgleaner.domain.memory_citations import citation_index
from litgleaner.domain.memory_policy import CandidateSignals, MemoryPolicy, score
from litgleaner.domain.memory_projection import project_facts
from litgleaner.ports.embedding import EmbeddingSnapshot
from litgleaner.ports.memory_candidates import CandidateSnapshot

_INTENTS = {"method", "methods", "methodology", "extends", "extension"}


@dataclass(frozen=True)
class _Basis:
    papers: Mapping[str, MemoryPaper]
    facts: Mapping[str, PaperFacts]
    vector: VectorProjection
    text: Mapping[str, float]
    hits: Mapping[str, tuple[TextEvidence, ...]]
    warnings: tuple[MemoryWarning, ...]


def _intent(facts: PaperFacts) -> bool:
    return any(
        any(isinstance(text, str) and text.strip() for text in item.contexts)
        and any(normalize_concept_name(intent) in _INTENTS for intent in item.intents)
        for item in (*facts.cites, *facts.cited_by)
    )


def _vector(
    paper_id: str, other: str, projection: VectorProjection
) -> tuple[float | None, VectorEvidence | None]:
    model = projection.model
    if (
        model is None
        or model.producer_version is None
        or paper_id not in projection.vectors
        or other not in projection.vectors
    ):
        return None, None
    value = cosine(projection.vectors[paper_id], projection.vectors[other])
    return value, VectorEvidence(
        model.producer_version,
        projection.inputs[paper_id].input_hash,
        projection.inputs[other].input_hash,
    )


def _signals(facts: PaperFacts, similarity: float | None, text: float | None) -> CandidateSignals:
    return CandidateSignals(
        direct_citation=bool(facts.cites or facts.cited_by),
        citation_intent=_intent(facts),
        common_references=len(facts.common_references),
        core_concepts=len(facts.core_concepts),
        methods=len(facts.methods),
        datasets=len(facts.datasets),
        cosine=similarity,
        text=text,
    )


def _warnings(
    paper_id: str, projection: VectorProjection, warnings: tuple[MemoryWarning, ...]
) -> tuple[MemoryWarning, ...]:
    found = [*warnings, *(MemoryWarning(w.paper_id, w.code, w.detail) for w in projection.warnings)]
    if projection.model is None or not projection.model.available:
        found.append(
            MemoryWarning(paper_id, "embedding_unavailable", "本地向量未启用或未就绪，使用其他信号")
        )
    elif paper_id not in projection.vectors:
        found.append(MemoryWarning(paper_id, "missing_embedding", "本篇没有当前向量，使用其他信号"))
    return tuple(dict.fromkeys(found))


def _basis(ctx: AppContext, paper_id: str, data: CandidateSnapshot) -> _Basis:
    papers = paper_views(data.facts)
    if paper_id not in papers:
        raise NotFound(f"论文尚未进入文库或不存在：{paper_id}")
    index = citation_index(
        data.facts.citations, data.facts.aliases, {p.id: p.title for p in data.facts.papers}
    )
    signals, warnings = fact_signals(ctx, data.facts)
    facts = {item.paper.paper_id: item for item in project_facts(paper_id, papers, index, signals)}
    vector = project_vectors(
        ctx,
        EmbeddingSnapshot(
            data.facts.papers,
            data.facts.fulltexts,
            data.facts.extractions,
            data.embeddings,
        ),
    )
    text, hits = text_signals(paper_id, data, signals)
    return _Basis(papers, facts, vector, text, hits, _warnings(paper_id, vector, warnings))


def _rank(paper_id: str, basis: _Basis, policy: MemoryPolicy) -> tuple[MemoryCandidate, ...]:
    matches = []
    ids = set(basis.facts) | basis.text.keys()
    if paper_id in basis.vector.vectors:
        ids.update(basis.vector.vectors)
    for other in ids - {paper_id}:
        fact = basis.facts.get(other, PaperFacts(basis.papers[other], (), (), (), (), (), ()))
        similarity, provenance = _vector(paper_id, other, basis.vector)
        values = _signals(fact, similarity, basis.text.get(other))
        result = score(values, policy)
        if result.total > 0 and result.total >= policy.minimum_score:
            matches.append(
                MemoryCandidate(fact, values, result, provenance, basis.hits.get(other, ()))
            )
    matches.sort(key=lambda item: (-item.score.total, item.facts.paper.paper_id))
    return tuple(matches)


def candidates(ctx: AppContext, paper_id: str, *, limit: int | None = None) -> MemoryCandidates:
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 200):
        raise InvalidRequest("候选查询数量必须为 1–200")
    policy = ctx.settings.memory_policy
    data = ctx.store.memory_candidates_snapshot(paper_id, policy.query_terms_limit)
    basis = _basis(ctx, paper_id, data)
    matches = _rank(paper_id, basis, policy)
    count = min(limit if limit is not None else policy.max_pairs, policy.max_pairs)
    return MemoryCandidates(
        basis.papers[paper_id],
        policy.policy_hash,
        data.terms,
        matches[:count],
        len(matches),
        count,
        basis.vector.model,
        basis.warnings,
    )
