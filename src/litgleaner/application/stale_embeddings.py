"""历史本地向量的只读核验，缺模型文件不能冒充输入失效。"""

from litgleaner.application.embedding_inputs import inputs
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.embedding import EmbeddingModel, PaperEmbedding, unpack_vector
from litgleaner.domain.stale import StaleItem, item


def _producer_checks(
    record: PaperEmbedding, model: EmbeddingModel | None
) -> tuple[list[str], list[str]]:
    if model is None:
        return [], ["未启用本地嵌入器，无法完整核对生产者"]
    reasons = []
    unchecked = []
    if (
        record.configuration_version != model.configuration_version
        or record.model != model.model
        or record.dimensions != model.dimensions
    ):
        reasons.append("嵌入模型、维度或推理配置已变化")
    if model.producer_version is None or not model.available:
        unchecked.append("本地嵌入器未就绪，无法完整核对模型资产与运行库")
    if model.producer_version is not None and record.embedder_version != model.producer_version:
        reasons.append("嵌入模型资产或运行库版本已变化")
    return reasons, unchecked


def check_embeddings(basis: StaleInputs) -> tuple[StaleItem, ...]:
    snapshot = basis.snapshot
    expected = inputs(
        basis.ctx, snapshot.papers, snapshot.fulltexts, tuple(basis.extractions.values())
    )
    model = basis.ctx.embedder.describe() if basis.ctx.embedder else None
    result = []
    for record in snapshot.embeddings:
        reasons, unchecked = _producer_checks(record, model)
        source = expected.get(record.paper_id)
        if source is None:
            unchecked.append("向量的原论文不存在，无法重建输入")
        elif record.input_hash != source.input_hash or record.input_text != source.input_text:
            reasons.append("嵌入实际输入文本或预处理协议已变化")
        if source is not None and source.source_extraction_id is not None:
            unchecked.extend(
                warning.detail
                for warning in source.warnings
                if warning.code == "extraction_producer_unchecked"
            )
        try:
            unpack_vector(record.vector, record.dimensions)
        except ValueError:
            reasons.append("向量字节、维度、数值或范数无效")
        selected = (
            record.paper_id in snapshot.embedding_library_ids
            and model is not None
            and model.available
            and model.producer_version is not None
            and not reasons
        )
        result.append(
            item(
                "embedding",
                f"{record.paper_id}:{record.input_hash}:{record.embedder_version}",
                record.paper_id,
                None,
                reasons=reasons,
                unchecked=unchecked,
                selected=selected,
            )
        )
    return tuple(result)
