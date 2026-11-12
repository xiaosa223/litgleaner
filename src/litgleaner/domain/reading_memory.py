"""阅读时展示的原始笔记、双向引用及已选关系；不参与状态核验。"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Literal

from litgleaner.domain.citation import Citation
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.memory import MemoryPaper, MemoryWarning
from litgleaner.domain.memory_citations import CitationIndex
from litgleaner.domain.paper_relation import SYMMETRIC_KINDS, PaperRelation
from litgleaner.domain.reading import Annotation

RelationDirection = Literal["outgoing", "incoming", "symmetric"]


@dataclass(frozen=True)
class UserNoteExcerpt:
    source_id: str
    paper_id: str
    text: str
    page: int | None
    source_version: str
    truncated: bool
    authorship: Literal["user"] = "user"


@dataclass(frozen=True)
class ReadingRelation:
    relation: PaperRelation
    paper: MemoryPaper
    direction: RelationDirection
    note: UserNoteExcerpt | None


@dataclass(frozen=True)
class ReadingCitation:
    paper: MemoryPaper
    cites: tuple[Citation, ...]
    cited_by: tuple[Citation, ...]
    note: UserNoteExcerpt | None


@dataclass(frozen=True)
class ReadingMemory:
    paper: MemoryPaper
    relations: tuple[ReadingRelation, ...]
    citations: tuple[ReadingCitation, ...]
    total: int
    limit: int
    offset: int
    relation_candidates: int
    relations_checked: int
    relations_truncated: bool
    warnings: tuple[MemoryWarning, ...]


def _excerpt(body: str) -> tuple[str, bool] | None:
    lines = body.splitlines()
    for position, line in enumerate(lines):
        if text := line.strip():
            return text[:240], len(text) > 240 or position + 1 < len(lines)
    return None


def note_excerpts(annotations: tuple[Annotation, ...]) -> dict[str, UserNoteExcerpt]:
    """选取每篇论文最新非空用户正文，不以作者quote替代。"""
    result: dict[str, UserNoteExcerpt] = {}
    ordered = sorted(annotations, key=lambda item: (item.updated_at, item.id), reverse=True)
    for annotation in ordered:
        if annotation.paper_id in result or annotation.body is None:
            continue
        excerpt = _excerpt(annotation.body)
        if excerpt is not None:
            text, truncated = excerpt
            result[annotation.paper_id] = UserNoteExcerpt(
                annotation.id,
                annotation.paper_id,
                text,
                annotation.page,
                stable_hash(asdict(annotation)),
                truncated,
            )
    return result


def _citation_pairs(index: CitationIndex) -> dict[tuple[str, str], tuple[Citation, ...]]:
    # 显式事实沿原ID保留；动态边与共同目标共用最终接受的分组。
    result = {
        pair: tuple(item for item in evidence if item.cited_paper_id is not None)
        for pair, evidence in index.direct.items()
    }
    for citing, grouped in index.by_paper.items():
        for key, evidence in grouped.items():
            target = index.targets.get(key)
            if target is not None and target.paper_id is not None:
                result[citing, target.paper_id] = evidence
    return result


def citation_links(
    paper_id: str,
    papers: Mapping[str, MemoryPaper],
    index: CitationIndex,
    notes: Mapping[str, UserNoteExcerpt],
) -> tuple[ReadingCitation, ...]:
    """按文库对端稳定分组，两个方向均保留完整来源证据。"""
    if paper_id not in papers:
        return ()
    pairs = _citation_pairs(index)
    result = []
    for peer in sorted(papers):
        if peer == paper_id:
            continue
        cites = pairs.get((paper_id, peer), ())
        cited_by = pairs.get((peer, paper_id), ())
        if cites or cited_by:
            result.append(ReadingCitation(papers[peer], cites, cited_by, notes.get(peer)))
    return tuple(result)


def relation_cards(
    paper_id: str,
    papers: Mapping[str, MemoryPaper],
    relations: tuple[PaperRelation, ...],
    notes: Mapping[str, UserNoteExcerpt],
) -> tuple[ReadingRelation, ...]:
    """保持调用方已经核验并选好的顺序与原始关系，不翻转双方证据。"""
    if paper_id not in papers:
        return ()
    result = []
    for relation in relations:
        if paper_id not in (relation.paper_a, relation.paper_b):
            continue
        peer = relation.paper_b if relation.paper_a == paper_id else relation.paper_a
        if peer == paper_id or peer not in papers:
            continue
        direction: RelationDirection = (
            "symmetric"
            if relation.kind in SYMMETRIC_KINDS
            else "outgoing"
            if relation.paper_a == paper_id
            else "incoming"
        )
        result.append(ReadingRelation(relation, papers[peer], direction, notes.get(peer)))
    return tuple(result)
