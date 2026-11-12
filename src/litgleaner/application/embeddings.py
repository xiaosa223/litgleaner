"""显式批量本地重算及只读余弦投影；不调用收费模型或来源。"""

from collections.abc import Mapping
from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.embedding_inputs import inputs
from litgleaner.domain.embedding import (
    EmbeddingBatch,
    EmbeddingModel,
    PaperEmbedding,
    cosine,
    pack_vector,
    unpack_vector,
)
from litgleaner.domain.embedding_views import (
    EmbeddingInput,
    EmbeddingWarning,
    ReembedPlan,
    ReembedResult,
    SimilarPaper,
    SimilarPapers,
)
from litgleaner.domain.errors import ConfigError, InvalidRequest, NotFound
from litgleaner.ports.embedding import EmbeddingSnapshot


@dataclass(frozen=True)
class _Basis:
    snapshot: EmbeddingSnapshot
    inputs: Mapping[str, EmbeddingInput]
    model: EmbeddingModel | None


@dataclass(frozen=True)
class VectorProjection:
    model: EmbeddingModel | None
    inputs: Mapping[str, EmbeddingInput]
    vectors: Mapping[str, tuple[float, ...]]
    warnings: tuple[EmbeddingWarning, ...]


def project_vectors(ctx: AppContext, data: EmbeddingSnapshot) -> VectorProjection:
    """使用调用者的一致快照，不额外查询或计算向量。"""
    source = inputs(ctx, data.papers, data.fulltexts, data.extractions)
    model = ctx.embedder.describe() if ctx.embedder else None
    vectors, corrupt = _vectors(_Basis(data, source, model))
    warnings = tuple(w for item in source.values() for w in item.warnings)
    return VectorProjection(model, source, vectors, (*warnings, *corrupt))


def _basis(ctx: AppContext) -> _Basis:
    data = ctx.store.embeddings.snapshot()
    return _Basis(
        data,
        inputs(ctx, data.papers, data.fulltexts, data.extractions),
        ctx.embedder.describe() if ctx.embedder else None,
    )


def _current(
    basis: _Basis,
    record: PaperEmbedding,
    source: EmbeddingInput,
) -> bool:
    model = basis.model
    return bool(
        model is not None
        and model.available
        and model.producer_version is not None
        and record.embedder_version == model.producer_version
        and record.configuration_version == model.configuration_version
        and record.model == model.model
        and record.dimensions == model.dimensions
        and record.input_hash == source.input_hash
        and record.input_text == source.input_text
    )


def _vectors(basis: _Basis) -> tuple[dict[str, tuple[float, ...]], tuple[EmbeddingWarning, ...]]:
    found: dict[str, tuple[float, ...]] = {}
    warnings = []
    if basis.model is None or not basis.model.available or basis.model.producer_version is None:
        return found, ()
    for record in basis.snapshot.embeddings:
        source = basis.inputs.get(record.paper_id)
        if (
            source is None
            or record.input_hash != source.input_hash
            or record.embedder_version != basis.model.producer_version
        ):
            continue
        if not _current(basis, record, source):
            warnings.append(
                EmbeddingWarning(
                    record.paper_id,
                    "invalid_embedding_record",
                    "当前向量指纹对应的元数据不一致，未参与相似度",
                )
            )
            continue
        try:
            found[record.paper_id] = unpack_vector(record.vector, record.dimensions)
        except ValueError:
            warnings.append(
                EmbeddingWarning(record.paper_id, "invalid_vector", "当前向量损坏，未参与相似度")
            )
    return found, tuple(warnings)


def _plan(basis: _Basis) -> ReembedPlan:
    valid, corrupt = _vectors(basis)
    pending = tuple(
        key for key, source in basis.inputs.items() if source.input_text and key not in valid
    )
    skipped = tuple(key for key, source in basis.inputs.items() if not source.input_text)
    warnings = tuple(w for source in basis.inputs.values() for w in source.warnings)
    return ReembedPlan(
        basis.model, len(basis.inputs), len(valid), pending, skipped, (*warnings, *corrupt)
    )


def preview(ctx: AppContext) -> ReembedPlan:
    return _plan(_basis(ctx))


def _records(
    ctx: AppContext,
    sources: tuple[EmbeddingInput, ...],
    batch: EmbeddingBatch,
    expected: EmbeddingModel,
) -> tuple[PaperEmbedding, ...]:
    model = batch.model
    if (
        not model.available
        or not model.producer_version
        or model.configuration_version != expected.configuration_version
        or model.model != expected.model
        or model.dimensions != expected.dimensions
        or len(batch.vectors) != len(sources)
    ):
        raise ConfigError("本地嵌入器返回的模型、版本、维度或向量数量不一致")
    try:
        encoded = [pack_vector(vector, model.dimensions) for vector in batch.vectors]
    except ValueError as error:
        raise ConfigError(str(error)) from error
    return tuple(
        PaperEmbedding(
            source.paper_id,
            source.input_hash,
            model.producer_version,
            model.configuration_version,
            model.model,
            model.dimensions,
            source.input_text,
            raw,
            source.source_extraction_id,
            ctx.now_iso(),
        )
        for source, raw in zip(sources, encoded, strict=True)
    )


def _save(ctx: AppContext, records: tuple[PaperEmbedding, ...]) -> tuple[int, int, tuple[str, ...]]:
    created, reused, changed = 0, 0, []
    with ctx.store.atomic():
        current = _basis(ctx)
        corrupt = {warning.paper_id for warning in _vectors(current)[1]}
        for record in records:
            source = current.inputs.get(record.paper_id)
            if source is None or not _current(current, record, source):
                changed.append(record.paper_id)
            elif record.paper_id in corrupt:
                raise ConfigError(
                    "当前向量指纹的已有记录损坏，不能覆盖只追加的历史，请检查或还原数据库"
                )
            elif ctx.store.embeddings.add(record):
                created += 1
            else:
                reused += 1
    return created, reused, tuple(changed)


def _reembed(ctx: AppContext, basis: _Basis, *, allow_download: bool) -> ReembedResult:
    plan = _plan(basis)
    if ctx.embedder is None or basis.model is None:
        raise ConfigError("本地向量已关闭，请先启用记忆配置中的嵌入器")
    if any(
        warning.code in {"invalid_vector", "invalid_embedding_record"} for warning in plan.warnings
    ):
        raise ConfigError("当前向量损坏且历史指纹只追加，请先检查或还原数据库，不能覆盖该记录")
    created, reused = 0, plan.cached
    changed: list[str] = []
    size = ctx.settings.embedding.batch_size
    for offset in range(0, len(plan.pending_paper_ids), size):
        sources = tuple(basis.inputs[key] for key in plan.pending_paper_ids[offset : offset + size])
        texts = tuple(source.input_text for source in sources)
        batch = (
            ctx.embedder.embed(texts)
            if allow_download
            else ctx.embedder.embed(texts, allow_download=False)
        )
        records = _records(ctx, sources, batch, basis.model)
        added, existing, skipped = _save(ctx, records)
        created += added
        reused += existing
        changed.extend(skipped)
    return ReembedResult(plan, created, reused, tuple(changed))


def reembed(ctx: AppContext) -> ReembedResult:
    return _reembed(ctx, _basis(ctx), allow_download=True)


def reembed_papers(ctx: AppContext, paper_ids: tuple[str, ...]) -> ReembedResult:
    """仅计算指定文库论文，资产必须已就绪，不准备或下载模型。"""
    basis = _basis(ctx)
    selected = {key: value for key, value in basis.inputs.items() if key in paper_ids}
    if basis.model is None or not basis.model.available:
        raise ConfigError("本地嵌入模型未就绪；自动入库不会下载资产")
    return _reembed(ctx, _Basis(basis.snapshot, selected, basis.model), allow_download=False)


def similar(ctx: AppContext, paper_id: str, *, limit: int = 5) -> SimilarPapers:
    if not 1 <= limit <= 200:
        raise InvalidRequest("相似论文数量必须为 1–200")
    basis = _basis(ctx)
    if paper_id not in basis.inputs:
        raise NotFound(f"论文尚未进入文库或不存在：{paper_id}")
    warnings = [w for source in basis.inputs.values() for w in source.warnings]
    if basis.model is None or not basis.model.available:
        warnings.append(
            EmbeddingWarning(
                paper_id, "embedding_unavailable", "本地嵌入模型未启用或未就绪，向量信号暂缺"
            )
        )
        return SimilarPapers(paper_id, basis.model, (), 0, tuple(warnings))
    vectors, corrupt = _vectors(basis)
    warnings.extend(corrupt)
    if paper_id not in vectors:
        warnings.append(
            EmbeddingWarning(
                paper_id, "missing_embedding", "缺少当前向量，可显式执行 memory reembed"
            )
        )
        return SimilarPapers(paper_id, basis.model, (), 0, tuple(warnings))
    titles = {paper.id: paper.title for paper in basis.snapshot.papers}
    matches = sorted(
        (
            SimilarPaper(key, titles[key], cosine(vectors[paper_id], vector))
            for key, vector in vectors.items()
            if key != paper_id
        ),
        key=lambda item: (-item.cosine, item.paper_id),
    )
    return SimilarPapers(
        paper_id, basis.model, tuple(matches[:limit]), len(matches), tuple(warnings)
    )
