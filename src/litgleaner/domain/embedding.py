"""本地向量协议、不可变记录及不依赖数值库的 float32 校验。"""

import math
import struct
from dataclasses import dataclass

from pydantic import Field

from litgleaner.domain.base import FrozenModel

INPUT_VERSION = "paper-embedding-v1"


class EmbeddingSettings(FrozenModel):
    max_input_chars: int = Field(default=8000, ge=100, le=100000)
    batch_size: int = Field(default=16, ge=1, le=256)


@dataclass(frozen=True)
class EmbeddingModel:
    model: str
    dimensions: int
    configuration_version: str
    producer_version: str | None
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class EmbeddingBatch:
    model: EmbeddingModel
    vectors: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class PaperEmbedding:
    paper_id: str
    input_hash: str
    embedder_version: str
    configuration_version: str
    model: str
    dimensions: int
    input_text: str
    vector: bytes
    source_extraction_id: int | None
    created_at: str


def unpack_vector(raw: bytes, dimensions: int) -> tuple[float, ...]:
    if type(dimensions) is not int or not 1 <= dimensions <= 65536 or len(raw) != dimensions * 4:
        raise ValueError("向量维度与 float32 字节长度不符")
    values = struct.unpack(f"<{dimensions}f", raw)
    if not all(math.isfinite(value) for value in values) or not any(values):
        raise ValueError("向量必须包含有限值且范数不能为零")
    return values


def pack_vector(values: tuple[float, ...], dimensions: int) -> bytes:
    if (
        type(dimensions) is not int
        or not 1 <= dimensions <= 65536
        or len(values) != dimensions
        or any(isinstance(value, bool) for value in values)
    ):
        raise ValueError("嵌入器返回的向量维度或数值无效")
    try:
        raw = struct.pack(f"<{dimensions}f", *values)
    except (OverflowError, struct.error, TypeError) as error:
        raise ValueError("嵌入器返回的数值无法保存为 float32") from error
    unpack_vector(raw, dimensions)
    return raw


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    if not left or len(left) != len(right):
        raise ValueError("余弦计算需要相同的非空维度")
    if not all(math.isfinite(value) for value in (*left, *right)):
        raise ValueError("余弦计算只接受有限数值")
    a, b = max(map(abs, left)), max(map(abs, right))
    if not a or not b:
        raise ValueError("余弦计算不接受零向量")
    first, second = tuple(x / a for x in left), tuple(y / b for y in right)
    norm_a, norm_b = math.hypot(*first), math.hypot(*second)
    score = math.fsum((x / norm_a) * (y / norm_b) for x, y in zip(first, second, strict=True))
    return max(-1.0, min(1.0, score))
