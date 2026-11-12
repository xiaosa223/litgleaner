"""问答候选共用扫描配额，只从规范来源取连续片段。"""

from collections.abc import Mapping
from dataclasses import dataclass, replace

from litgleaner.application.qa_context import _pieces, _terms
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.document import Block
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.qa import Question
from litgleaner.domain.qa_memory import QASource
from litgleaner.domain.sections import Section, split_sections


@dataclass
class ScanBudget:
    chars: int = 2_000_000
    sources: int = 4000

    def pieces(
        self, text: str, *, chars: int | None = None, maximum: int = 4000
    ) -> tuple[str, ...]:
        allowed = min(self.chars, len(text), self.chars if chars is None else chars)
        if allowed <= 0 or self.sources <= 0:
            return ()
        self.chars -= allowed
        pieces = _pieces(text[:allowed])[: min(self.sources, maximum)]
        self.sources -= max(1, len(pieces))
        return pieces

    def whole(self, text: str) -> bool:
        if not text.strip() or len(text) > self.chars or self.sources <= 0:
            return False
        self.chars -= len(text)
        self.sources -= 1
        return True


@dataclass(frozen=True)
class Candidate:
    source: QASource
    lexical: float
    fts: float = 0.0


def lexical_score(text: str, question: Question) -> float:
    terms = _terms(question.text + " " + (question.selection or ""))
    return len(terms & _terms(text)) / max(1, len(terms))


def author_source(paper_id: str, page: int, text: str, doc_key: str) -> QASource:
    return QASource(
        f"author:{paper_id}:{page}:{stable_hash(text)[:16]}",
        paper_id,
        "author",
        text,
        page,
        doc_key,
    )


def document_candidates(
    basis: StaleInputs,
    question: Question,
    paper_id: str,
    budget: ScanBudget,
    *,
    max_chars: int = 1_000_000,
    max_sources: int = 2000,
) -> tuple[Candidate, ...]:
    document = basis.document(paper_id)
    state = basis.snapshot.fulltexts[paper_id]
    assert state.doc_key is not None
    blocks: list[Block] = []
    remaining = min(max_chars, budget.chars)
    for block in document.blocks[:4000]:
        if remaining <= 0:
            break
        blocks.append(replace(block, text=block.text[:remaining]))
        remaining -= len(blocks[-1].text)
    sections = split_sections(replace(document, blocks=tuple(blocks)))
    selected = (
        {
            section.path
            for section in sections
            if any(block.page == question.page for block in section.blocks)
        }
        if paper_id == question.paper_id and question.page is not None
        else set()
    )
    return _section_candidates(
        basis, question, paper_id, sections, budget, maximum=max_sources, selected=selected
    )


def _section_candidates(
    basis: StaleInputs,
    question: Question,
    paper_id: str,
    sections: tuple[Section, ...],
    budget: ScanBudget,
    *,
    maximum: int,
    selected: set[tuple[str, ...]],
) -> tuple[Candidate, ...]:
    state = basis.snapshot.fulltexts[paper_id]
    assert state.doc_key is not None
    found: list[Candidate] = []
    for section in sections:
        for block in section.blocks:
            if len(found) >= maximum or budget.chars <= 0 or budget.sources <= 0:
                return tuple(found)
            for piece in budget.pieces(block.text, maximum=maximum - len(found)):
                source = author_source(paper_id, block.page, piece, state.doc_key)
                score = lexical_score(piece, question)
                score += 0.25 * (section.path in selected)
                score += 0.1 * (paper_id == question.paper_id and block.page == question.page)
                found.append(Candidate(source, score))
                if len(found) >= maximum:
                    return tuple(found)
    return tuple(found)


def balanced_candidates(
    candidates: Mapping[str, Candidate], paper_id: str, *, limit: int = 64
) -> tuple[Candidate, ...]:
    ranked = sorted(
        candidates.values(), key=lambda item: (-item.lexical - item.fts, item.source.id)
    )
    own = [item for item in ranked if item.source.paper_id == paper_id]
    other = [item for item in ranked if item.source.paper_id != paper_id]
    selected = own[: limit // 2] + other[: limit - min(len(own), limit // 2)]
    ids = {item.source.id for item in selected}
    selected.extend(item for item in own if item.source.id not in ids)
    return tuple(selected[:limit])
