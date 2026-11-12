"""全局词法结果与当前文库向量混排；任何向量故障保留原词法投影。"""

import unicodedata
from dataclasses import dataclass
from typing import Literal

from litgleaner.application.context import AppContext
from litgleaner.application.embeddings import VectorProjection, project_vectors
from litgleaner.application.query_vectors import embed, identity
from litgleaner.domain.embedding import EmbeddingModel, cosine
from litgleaner.domain.errors import ConfigError
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.search_hybrid import (
    SEARCH_HYBRID_PROTOCOL,
    SearchRank,
    SearchSignal,
    contribution,
    fuse,
)
from litgleaner.domain.search_text import search_excerpt
from litgleaner.ports.search import SearchCandidate, SearchHit, SearchSnapshot


@dataclass(frozen=True)
class SearchResults:
    hits: tuple[SearchHit, ...]
    mode: Literal["lexical", "hybrid"] = "lexical"
    protocol: str = SEARCH_HYBRID_PROTOCOL
    model: EmbeddingModel | None = None
    query_hash: str = ""
    signals: tuple[SearchRank, ...] = ()
    warnings: tuple[str, ...] = ()


def _warnings(values: tuple[str, ...]) -> tuple[str, ...]:
    unique = tuple(dict.fromkeys(value[:500] for value in values))
    return unique[:32] + ((f"另有{len(unique) - 32}条诊断已省略",) if len(unique) > 32 else ())


def _hit(row: SearchCandidate, terms: tuple[str, ...]) -> SearchHit:
    return SearchHit(
        row.kind,
        row.id,
        row.paper_id,
        row.title,
        search_excerpt(row.text, list(terms)),
        row.page,
        row.authorship,
    )


def _lexical(
    data: SearchSnapshot,
    limit: int,
    query_hash: str,
    warnings: tuple[str, ...],
    model: EmbeddingModel | None = None,
) -> SearchResults:
    maximum = max((-row.bm25 for row in data.candidates), default=0.0)
    rows = data.candidates[:limit]
    return SearchResults(
        tuple(_hit(row, data.terms) for row in rows),
        model=model,
        query_hash=query_hash,
        signals=tuple(
            contribution(SearchSignal(row.kind, row.paper_id, row.id, row.bm25), maximum)
            for row in rows
        ),
        warnings=_warnings(warnings),
    )


def _project(
    ctx: AppContext,
    data: SearchSnapshot,
) -> tuple[VectorProjection | None, tuple[str, ...]]:
    if data.embedding_problem or data.embeddings is None:
        return None, ("本地向量快照不可用，已保留词法结果",)
    projected = project_vectors(ctx, data.embeddings)
    warnings = tuple(f"{row.paper_id}：{row.detail}" for row in projected.warnings)
    if (
        projected.model is None
        or not projected.model.available
        or not projected.model.producer_version
    ):
        return projected, (*warnings, "本地向量未启用或未就绪，已保留词法结果")
    if not projected.vectors:
        return projected, (*warnings, "没有当前有效论文向量，已保留词法结果")
    return projected, warnings


def _combine(
    data: SearchSnapshot,
    projected: VectorProjection,
    query: tuple[float, ...],
    limit: int,
) -> tuple[tuple[SearchHit, ...], tuple[SearchRank, ...]]:
    scores = {key: cosine(query, value) for key, value in projected.vectors.items()}
    records: dict[tuple[str, str, str], SearchCandidate] = {}
    signals: list[SearchSignal] = []
    for row in data.candidates:
        key = row.kind, row.paper_id, row.id
        if key not in records or row.bm25 < records[key].bm25:
            records[key] = row
        value = scores.get(row.paper_id) if row.kind == "paper" else None
        source = projected.inputs.get(row.paper_id) if value is not None else None
        signals.append(SearchSignal(*key, row.bm25, value, source.input_hash if source else None))
    if data.embeddings is not None:
        for paper in data.embeddings.papers:
            value = scores.get(paper.id, 0.0)
            key = "paper", paper.id, paper.id
            if value > 0 and key not in records:
                records[key] = SearchCandidate(
                    "paper",
                    paper.id,
                    paper.id,
                    paper.title,
                    paper.abstract or "",
                    None,
                    "author",
                    0.0,
                )
                signals.append(
                    SearchSignal(*key, None, value, projected.inputs[paper.id].input_hash)
                )
    ranked = fuse(tuple(signals))[:limit]
    return tuple(
        _hit(records[(row.kind, row.paper_id, row.id)], data.terms) for row in ranked
    ), ranked


def run(ctx: AppContext, query: str, limit: int) -> SearchResults:
    data = ctx.store.search.snapshot(query, limit=512)
    text = " ".join(unicodedata.normalize("NFKC", query).split())[
        : ctx.settings.embedding.max_input_chars
    ]
    query_hash = stable_hash(text)
    if not data.terms:
        return _lexical(data, limit, query_hash, ("查询没有可检索词，未执行向量推理",))
    model: EmbeddingModel | None = None
    warnings: tuple[str, ...] = ()
    try:
        initial = ctx.embedder.describe() if ctx.embedder else None
        projected, warnings = _project(ctx, data)
        model = projected.model if projected else initial
        if projected is None or model is None or not model.available or not projected.vectors:
            return _lexical(data, limit, query_hash, warnings, model)
        if initial is None or identity(model) != identity(initial):
            raise ConfigError("全局搜索模型身份已变化")
        (vector,) = embed(ctx, (text,), model)
        hits, signals = _combine(data, projected, vector, limit)
        if ctx.embedder is None or identity(ctx.embedder.describe()) != identity(model):
            raise ConfigError("全局搜索融合后的模型身份已变化")
        return SearchResults(
            hits[:limit],
            "hybrid",
            model=model,
            query_hash=query_hash,
            signals=signals[:limit],
            warnings=_warnings(warnings),
        )
    except Exception:
        return _lexical(
            data, limit, query_hash, (*warnings, "本地搜索向量未能完整核验，已保留词法结果"), model
        )
