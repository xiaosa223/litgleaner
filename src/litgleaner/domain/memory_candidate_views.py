"""可追溯的文库候选排序结果，语义关系由后续任务核实。"""

from dataclasses import dataclass
from typing import Literal

from litgleaner.domain.embedding import EmbeddingModel
from litgleaner.domain.memory import MemoryPaper, MemoryWarning, PaperFacts
from litgleaner.domain.memory_policy import CandidateScore, CandidateSignals


@dataclass(frozen=True)
class TextEvidence:
    kind: Literal["paper", "fulltext", "extraction"]
    source_id: str
    page: int | None
    source_version: str
    bm25: float


@dataclass(frozen=True)
class VectorEvidence:
    embedder_version: str
    here_input_hash: str
    there_input_hash: str


@dataclass(frozen=True)
class MemoryCandidate:
    facts: PaperFacts
    signals: CandidateSignals
    score: CandidateScore
    vector: VectorEvidence | None
    text_hits: tuple[TextEvidence, ...]


@dataclass(frozen=True)
class MemoryCandidates:
    paper: MemoryPaper
    policy_hash: str
    query_terms: tuple[str, ...]
    matches: tuple[MemoryCandidate, ...]
    total: int
    limit: int
    embedding_model: EmbeddingModel | None
    warnings: tuple[MemoryWarning, ...]
