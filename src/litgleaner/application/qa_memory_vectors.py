"""问句向量仅使用已就绪资产；模型竞态或无效向量使本次检索降级。"""

from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.embeddings import project_vectors
from litgleaner.application.qa_memory_candidates import Candidate
from litgleaner.application.query_vectors import embed as _embed
from litgleaner.application.query_vectors import identity as _identity
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.embedding import EmbeddingModel, cosine
from litgleaner.domain.errors import ConfigError
from litgleaner.domain.qa import Question
from litgleaner.ports.embedding import EmbeddingSnapshot

_FAILED = "本地问答向量未能完整核验，已使用词法、FTS与有效关系"
_UNAVAILABLE = "本地问答向量未就绪，已使用词法、FTS与有效关系"


@dataclass(frozen=True)
class VectorQuery:
    model: EmbeddingModel
    vector: tuple[float, ...]
    paper_ids: tuple[str, ...]


def prepare(
    ctx: AppContext, question: Question, basis: StaleInputs
) -> tuple[VectorQuery | None, tuple[str, ...]]:
    try:
        model = ctx.embedder.describe() if ctx.embedder else None
        if model is None or not model.available or model.producer_version is None:
            return None, (_UNAVAILABLE,)
        text = (question.text + " " + (question.selection or ""))[
            : ctx.settings.embedding.max_input_chars
        ]
        (vector,) = _embed(ctx, (text,), model)
        allowed = basis.snapshot.library_paper_ids | {question.paper_id}
        data = EmbeddingSnapshot(
            tuple(p for p in basis.snapshot.papers if p.id in allowed),
            basis.snapshot.fulltexts,
            tuple(basis.extractions.values()),
            basis.snapshot.embeddings,
        )
        projected = project_vectors(ctx, data)
        if projected.model is None or _identity(projected.model) != _identity(model):
            raise ConfigError("问答向量投影身份已变化")
        ranked = sorted(
            (
                (cosine(vector, candidate), paper_id)
                for paper_id, candidate in projected.vectors.items()
                if paper_id != question.paper_id
            ),
            key=lambda item: (-item[0], item[1]),
        )
        selected = tuple(paper_id for score, paper_id in ranked if score > 0)[:5]
        if ctx.embedder is None or _identity(ctx.embedder.describe()) != _identity(model):
            raise ConfigError("问答向量投影后的模型身份已变化")
        return VectorQuery(model, vector, selected), ()
    except Exception:
        return None, (_FAILED,)


def rerank(
    ctx: AppContext, query: VectorQuery | None, candidates: tuple[Candidate, ...]
) -> tuple[tuple[Candidate, ...], tuple[str, ...]]:
    base = {item.source.id: item.lexical + item.fts for item in candidates}
    warnings: tuple[str, ...] = ()
    authors = tuple(item for item in candidates if item.source.kind == "author")[:64]
    if query is not None and authors:
        try:
            vectors = _embed(ctx, tuple(item.source.text for item in authors), query.model)
            for item, vector in zip(authors, vectors, strict=True):
                base[item.source.id] += max(0.0, cosine(query.vector, vector))
        except Exception:
            warnings = (_FAILED,)
    return tuple(
        sorted(candidates, key=lambda item: (-base[item.source.id], item.source.id))
    ), warnings
