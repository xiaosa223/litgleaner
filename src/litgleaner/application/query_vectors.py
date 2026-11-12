"""临时检索推理共用模型身份与 float32 护栏，绝不准备或下载资产。"""

from litgleaner.application.context import AppContext
from litgleaner.domain.embedding import EmbeddingModel, pack_vector, unpack_vector
from litgleaner.domain.errors import ConfigError


def identity(model: EmbeddingModel) -> tuple[str, int, str, str | None, bool]:
    return (
        model.model,
        model.dimensions,
        model.configuration_version,
        model.producer_version,
        model.available,
    )


def embed(
    ctx: AppContext, texts: tuple[str, ...], expected: EmbeddingModel
) -> tuple[tuple[float, ...], ...]:
    if (
        ctx.embedder is None
        or not expected.available
        or not expected.producer_version
        or type(expected.dimensions) is not int
        or not 1 <= expected.dimensions <= 65536
        or identity(ctx.embedder.describe()) != identity(expected)
    ):
        raise ConfigError("本地检索嵌入模型未就绪或身份已变化")
    batch = ctx.embedder.embed(texts, allow_download=False)
    if (
        identity(batch.model) != identity(expected)
        or identity(ctx.embedder.describe()) != identity(expected)
        or len(batch.vectors) != len(texts)
    ):
        raise ConfigError("本地检索嵌入批次身份或数量不一致")
    return tuple(
        unpack_vector(pack_vector(vector, expected.dimensions), expected.dimensions)
        for vector in batch.vectors
    )
