"""按当前可用事实投影单篇论文的文库邻居，不生成语义判断。"""

from collections import defaultdict
from collections.abc import Mapping

from litgleaner.domain.knowledge import normalize_concept_name
from litgleaner.domain.memory import (
    FactSignals,
    FieldEvidence,
    MemoryPaper,
    PaperFacts,
    SharedConcept,
    SharedField,
    SharedReference,
)
from litgleaner.domain.memory_citations import CitationIndex


def _shared_fields(
    here: tuple[FieldEvidence, ...], there: tuple[FieldEvidence, ...]
) -> tuple[SharedField, ...]:
    left: dict[str, list[FieldEvidence]] = defaultdict(list)
    right: dict[str, list[FieldEvidence]] = defaultdict(list)
    for evidence in here:
        left[normalize_concept_name(evidence.text)].append(evidence)
    for evidence in there:
        right[normalize_concept_name(evidence.text)].append(evidence)
    return tuple(
        SharedField(key, tuple(left[key]), tuple(right[key]))
        for key in sorted(left.keys() & right.keys())
    )


def _shared_concepts(here: FactSignals, there: FactSignals) -> tuple[SharedConcept, ...]:
    left = {concept.id: (concept, occurrence) for concept, occurrence in here.core_concepts}
    right = {concept.id: occurrence for concept, occurrence in there.core_concepts}
    return tuple(
        SharedConcept(key, left[key][0].name, left[key][1], right[key])
        for key in sorted(left.keys() & right.keys())
    )


def project_facts(
    paper_id: str,
    papers: Mapping[str, MemoryPaper],
    citations: CitationIndex,
    signals: Mapping[str, FactSignals],
) -> tuple[PaperFacts, ...]:
    here = citations.by_paper.get(paper_id, {})
    current = signals.get(paper_id, FactSignals())
    matches: list[PaperFacts] = []
    for other_id in sorted(papers):
        if other_id == paper_id:
            continue
        there = citations.by_paper.get(other_id, {})
        common = tuple(
            SharedReference(citations.targets[key], here[key], there[key])
            for key in sorted(here.keys() & there.keys())
        )
        other = signals.get(other_id, FactSignals())
        match = PaperFacts(
            papers[other_id],
            citations.direct.get((paper_id, other_id), ()),
            citations.direct.get((other_id, paper_id), ()),
            common,
            _shared_concepts(current, other),
            _shared_fields(current.datasets, other.datasets),
            _shared_fields(current.methods, other.methods),
        )
        if any(
            (
                match.cites,
                match.cited_by,
                common,
                match.core_concepts,
                match.datasets,
                match.methods,
            )
        ):
            matches.append(match)
    return tuple(matches)
