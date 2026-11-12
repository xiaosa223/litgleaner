"""文库论文之间可追溯的事实信号；语义关系和策略评分另行核实。"""

from dataclasses import dataclass

from litgleaner.domain.citation import Citation
from litgleaner.domain.identity import Alias
from litgleaner.domain.knowledge import Concept, ConceptOccurrence


@dataclass(frozen=True)
class MemoryPaper:
    paper_id: str
    title: str
    reading_status: str


@dataclass(frozen=True)
class MemoryWarning:
    paper_id: str
    code: str
    detail: str


@dataclass(frozen=True)
class ReferenceTarget:
    paper_id: str | None
    aliases: tuple[Alias, ...]
    title: str | None


@dataclass(frozen=True)
class SharedReference:
    target: ReferenceTarget
    here: tuple[Citation, ...]
    there: tuple[Citation, ...]


@dataclass(frozen=True)
class SharedConcept:
    concept_id: str
    name: str
    here: ConceptOccurrence
    there: ConceptOccurrence


@dataclass(frozen=True)
class FieldEvidence:
    text: str
    quote: str
    page: int
    extraction_id: int
    input_hash: str
    producer_version: str


@dataclass(frozen=True)
class SharedField:
    normalized_name: str
    here: tuple[FieldEvidence, ...]
    there: tuple[FieldEvidence, ...]


@dataclass(frozen=True)
class FactSignals:
    core_concepts: tuple[tuple[Concept, ConceptOccurrence], ...] = ()
    datasets: tuple[FieldEvidence, ...] = ()
    methods: tuple[FieldEvidence, ...] = ()


@dataclass(frozen=True)
class PaperFacts:
    paper: MemoryPaper
    cites: tuple[Citation, ...]
    cited_by: tuple[Citation, ...]
    common_references: tuple[SharedReference, ...]
    core_concepts: tuple[SharedConcept, ...]
    datasets: tuple[SharedField, ...]
    methods: tuple[SharedField, ...]


@dataclass(frozen=True)
class MemoryFacts:
    paper: MemoryPaper
    matches: tuple[PaperFacts, ...]
    total: int
    limit: int
    offset: int
    warnings: tuple[MemoryWarning, ...]
