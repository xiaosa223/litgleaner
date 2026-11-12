"""按不同文库论文的精确引文，投影有原始证据的重要前作。"""

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from litgleaner.domain.citation import Citation
from litgleaner.domain.memory import MemoryPaper, ReferenceTarget
from litgleaner.domain.memory_citations import CitationIndex, TargetKey


@dataclass(frozen=True)
class ReferenceSupport:
    paper: MemoryPaper
    citations: tuple[Citation, ...]


@dataclass(frozen=True)
class ImportantWork:
    target: ReferenceTarget
    citing_count: int
    citing_papers: tuple[ReferenceSupport, ...]


@dataclass(frozen=True)
class ImportantWorks:
    library_size: int
    threshold: int
    works: tuple[ImportantWork, ...]
    total: int
    limit: int
    offset: int


def project_important_works(
    papers: Mapping[str, MemoryPaper], index: CitationIndex
) -> tuple[ImportantWork, ...]:
    if not papers:
        return ()
    threshold = (len(papers) + 2) // 3
    supports: dict[TargetKey, list[ReferenceSupport]] = defaultdict(list)
    for paper_id in sorted(papers):
        for key, evidence in index.by_paper.get(paper_id, {}).items():
            if evidence and index.targets[key].paper_id != paper_id:
                supports[key].append(ReferenceSupport(papers[paper_id], evidence))
    keys = sorted(supports, key=lambda key: (-len(supports[key]), key))
    return tuple(
        ImportantWork(index.targets[key], len(supports[key]), tuple(supports[key]))
        for key in keys
        if len(supports[key]) >= threshold
    )
