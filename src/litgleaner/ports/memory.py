"""论文记忆只读事实所需的一致快照。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from litgleaner.domain.citation import Citation
from litgleaner.domain.identity import Alias
from litgleaner.domain.knowledge import Concept, ConceptOccurrence
from litgleaner.domain.paper import Paper
from litgleaner.domain.reading import ReadingState
from litgleaner.ports.embedding import EmbeddingRepo
from litgleaner.ports.relations import AdmissionRepo, RelationRepo

if TYPE_CHECKING:
    from litgleaner.ports.memory_candidates import CandidateSnapshot
    from litgleaner.ports.store import Extraction, FullTextState


@dataclass(frozen=True)
class MemorySnapshot:
    papers: tuple[Paper, ...]
    aliases: Mapping[str, tuple[Alias, ...]]
    citations: tuple[Citation, ...]
    fulltexts: Mapping[str, FullTextState]
    extractions: tuple[Extraction, ...]
    concepts: tuple[Concept, ...]
    occurrences: tuple[ConceptOccurrence, ...]
    reading: Mapping[str, ReadingState]
    concept_names: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


class MemoryStore(Protocol):
    def memory_snapshot(self) -> MemorySnapshot: ...
    def memory_candidates_snapshot(
        self, paper_id: str, terms_limit: int = 32
    ) -> CandidateSnapshot: ...
    @property
    def embeddings(self) -> EmbeddingRepo: ...
    @property
    def admissions(self) -> AdmissionRepo: ...
    @property
    def relations(self) -> RelationRepo: ...
