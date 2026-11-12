"""本地嵌入：描述不加载推理引擎；实际计算可准备本地模型资产。"""

from typing import Protocol

from litgleaner.domain.embedding import EmbeddingBatch, EmbeddingModel


class Embedder(Protocol):
    def describe(self) -> EmbeddingModel: ...
    def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch: ...
