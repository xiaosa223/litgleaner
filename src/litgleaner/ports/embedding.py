"""文库向量的窄快照及只追加仓储。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from litgleaner.domain.embedding import PaperEmbedding
from litgleaner.domain.paper import Paper

if TYPE_CHECKING:
    from litgleaner.ports.store import Extraction, FullTextState


@dataclass(frozen=True)
class EmbeddingSnapshot:
    papers: tuple[Paper, ...]
    fulltexts: Mapping[str, FullTextState]
    extractions: tuple[Extraction, ...]
    embeddings: tuple[PaperEmbedding, ...]


class EmbeddingRepo(Protocol):
    def snapshot(self) -> EmbeddingSnapshot: ...
    def add(self, record: PaperEmbedding) -> bool: ...
    def for_paper(self, paper_id: str) -> tuple[PaperEmbedding, ...]: ...
