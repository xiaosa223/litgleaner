"""问答的有界本地词法检索和页级引文核验；后续可替换为混合检索。"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from litgleaner.domain.chunker import ChunkedDocument, estimate_tokens
from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.errors import InvalidRequest, PermanentError
from litgleaner.domain.evidence import clean_untrusted, select_chunks, verify_quote, wrap_untrusted
from litgleaner.domain.qa import Citation, Question, Statement
from litgleaner.domain.sections import split_sections

TOKEN_BUDGET = 4000
_MAX_SCAN_CHARS = 2_000_000
_MAX_SOURCES = 4000
_WORDS = re.compile(r"[a-zA-Z0-9]+|[\u3400-\u9fff]+")
_UNITS = re.compile(r"\S+\s*|\s+")


def qa_token_count(text: str) -> int:
    """QA 独立保守预算：至少每两字节一个 token，长词、公式和 Unicode 不能绕过。"""
    return max(estimate_tokens(text), (len(text.encode("utf-8")) + 1) // 2)


@dataclass(frozen=True)
class Source:
    page: int
    text: str


@dataclass(frozen=True)
class QAContext:
    text: str
    sources: tuple[Source, ...]


def page_text(document: NormalizedDocument, page: int) -> str:
    return "\n".join(block.text for block in document.blocks if block.page == page)


def validate_selection(
    document: NormalizedDocument, selection: str | None, page: int | None
) -> None:
    if page is not None and (
        not isinstance(page, int)
        or isinstance(page, bool)
        or page < 0
        or not any(block.page == page for block in document.blocks)
    ):
        raise InvalidRequest("选区页码不存在（页码从 0 开始）")
    if selection is not None:
        if page is None or not selection.strip():
            raise InvalidRequest("选区必须带有真实页码和非空原文")
        normalized = " ".join(selection.split()).casefold()
        if normalized not in " ".join(page_text(document, page).split()).casefold():
            raise InvalidRequest("选区原文不在指定页中")


def _terms(text: str) -> frozenset[str]:
    terms: set[str] = set()
    for match in _WORDS.findall(text.casefold()):
        if "\u3400" <= match[0] <= "\u9fff":
            terms.update(match[i : i + 2] for i in range(max(1, len(match) - 1)))
        else:
            terms.add(match)
    return frozenset(terms)


def _pieces(text: str, limit: int = 450) -> tuple[str, ...]:
    pieces: list[str] = []
    current = ""
    for unit in _UNITS.findall(text):
        # 一个 Unicode 字符最多四字节；按此上限硬切，原文连续片段不改写、不截掉后半段。
        width = max(1, limit // 2)
        for start in range(0, len(unit), width):
            piece = unit[start : start + width]
            if current and qa_token_count(current + piece) > limit:
                pieces.append(current.strip())
                current = ""
            current += piece
    return (*pieces, current.strip()) if current.strip() else tuple(pieces)


def _ranked_sources(
    document: NormalizedDocument,
    chunks: ChunkedDocument,
    query: str,
    selection: str | None,
    page: int | None,
) -> list[Source]:
    terms = _terms(query + " " + (selection or ""))
    fallback = {c.section_path for c in select_chunks(chunks.chunks, TOKEN_BUDGET)}
    sections = split_sections(document)
    selected_paths = {
        s.path
        for s in sections
        if any(
            b.page == page
            and selection
            and " ".join(selection.split()).casefold() in " ".join(b.text.split()).casefold()
            for b in s.blocks
        )
    }
    ranked: list[tuple[int, int, Source]] = []
    scanned = 0
    for section in sections:
        for block in section.blocks:
            if scanned >= _MAX_SCAN_CHARS or len(ranked) >= _MAX_SOURCES:
                break
            text = block.text[: _MAX_SCAN_CHARS - scanned]
            scanned += len(text)
            for piece in _pieces(text):
                if len(ranked) >= _MAX_SOURCES:
                    break
                score = 10 * len(terms.intersection(_terms(piece)))
                score += 5 * (section.path in selected_paths) + 2 * (block.page == page)
                score += int(section.path in fallback)
                ranked.append((score, len(ranked), Source(block.page, piece)))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    chosen = [Source(page, selection)] if selection is not None and page is not None else []
    return chosen + [source for _, _, source in ranked]


def select_context(
    document: NormalizedDocument,
    chunks: ChunkedDocument,
    query: str,
    selection: str | None = None,
    page: int | None = None,
    *,
    token_budget: int = TOKEN_BUDGET,
) -> QAContext:
    validate_selection(document, selection, page)
    sources: list[Source] = []
    rendered: list[str] = []
    for source in _ranked_sources(document, chunks, query, selection, page):
        if source in sources:
            continue
        text = f"[page={source.page}]\n{source.text}"
        if qa_token_count("\n\n".join((*rendered, text))) <= min(token_budget, TOKEN_BUDGET):
            sources.append(source)
            rendered.append(text)
    return QAContext("\n\n".join(rendered), tuple(sources))


def trim_context(context: QAContext) -> QAContext:
    """从末尾去掉最低优先级来源；保留先选中的原文片段及其文本。"""
    sources = context.sources[:-1]
    return QAContext("\n\n".join(f"[page={s.page}]\n{s.text}" for s in sources), sources)


def build_inputs(
    question: Question,
    document: NormalizedDocument,
    chunks: ChunkedDocument,
    profile: str,
    system: str,
) -> tuple[QAContext, str]:
    """构造模型实际收到的输入；在线问答与只读失效检查共用同一预算和上下文。"""
    data = {
        "paper_id": question.paper_id,
        "question": question.text,
        "selection": question.selection,
        "research_profile": profile,
    }
    baseline = system + wrap_untrusted(json.dumps(data, ensure_ascii=False))
    budget = TOKEN_BUDGET - qa_token_count(baseline) - 100
    context = select_context(
        document,
        chunks,
        question.text,
        question.selection,
        question.page,
        token_budget=max(0, budget),
    )
    if question.selection is not None and (
        not context.sources or context.sources[0].text != question.selection
    ):
        raise PermanentError("问答输入过长，无法容纳选区，请缩小范围后重新提问")
    while True:
        inputs = json.dumps({**data, "paper_sources": context.text}, ensure_ascii=False)
        if qa_token_count(system + wrap_untrusted(inputs)) <= TOKEN_BUDGET:
            return context, inputs
        if not context.sources or (question.selection is not None and len(context.sources) == 1):
            raise PermanentError("问答输入过长，请缩小问题或选区后重新提问")
        context = trim_context(context)


def verified_statements(
    paper_id: str,
    document: NormalizedDocument,
    context: QAContext,
    statements: Sequence[Mapping[str, Any]],
) -> tuple[Statement, ...]:
    results: list[Statement] = []
    for statement in statements:
        citations: list[Citation] = []
        for item in statement["citations"]:
            citation = Citation(item["paper_id"], item["page"], item["quote"])
            sent = "\n".join(s.text for s in context.sources if s.page == citation.page)
            if (
                citation.paper_id == paper_id
                and verify_quote(citation.quote, clean_untrusted(sent))
                and verify_quote(citation.quote, page_text(document, citation.page))
                and citation not in citations
            ):
                citations.append(citation)
        results.append(Statement(statement["text"], tuple(citations), not citations))
    return tuple(results)
