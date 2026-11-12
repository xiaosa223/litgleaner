"""候选排序的只读事实、向量和文本命中快照。"""

from dataclasses import dataclass
from typing import Literal

from litgleaner.domain.embedding import PaperEmbedding
from litgleaner.ports.memory import MemorySnapshot

CandidateTextKind = Literal["paper", "fulltext", "extraction"]


@dataclass(frozen=True)
class CandidateTextHit:
    paper_id: str
    kind: CandidateTextKind
    source_id: str
    page: int | None
    source_version: str
    bm25: float


@dataclass(frozen=True)
class CandidateSnapshot:
    facts: MemorySnapshot
    embeddings: tuple[PaperEmbedding, ...]
    terms: tuple[str, ...]
    text_hits: tuple[CandidateTextHit, ...]
