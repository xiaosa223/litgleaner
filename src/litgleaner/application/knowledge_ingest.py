"""概念摄取只登记可定位的术语；定义必须另经原文和实际发送内容双重核验。"""

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from litgleaner.application.context import AppContext
from litgleaner.application.documents import paper_header
from litgleaner.domain.chunker import ChunkedDocument, estimate_tokens
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.evidence import MIN_QUOTE_CHARS, clean_untrusted, select_chunks, verify_quote
from litgleaner.domain.knowledge import ConceptOccurrence, ConceptRole, normalize_concept_name
from litgleaner.domain.paper import Paper
from litgleaner.domain.reading import BBox

_ROLE_PRIORITY = {"core": 3, "prerequisite": 2, "mentioned": 1}


@dataclass(frozen=True)
class SentBlock:
    block: Block
    text: str

    @property
    def page(self) -> int:
        return self.block.page


@dataclass(frozen=True)
class ExtractionSource:
    text: str
    sources: tuple[SentBlock, ...]
    spans: tuple[tuple[int, int], ...] | None = None


def _legacy_spans(source: ExtractionSource) -> tuple[tuple[int, int], ...] | None:
    """旧来源只接受精确 marker 后缀结构，或明确只有一个正文块的消息。"""
    if len(source.sources) == 1 and source.text == source.sources[0].text:
        return ((0, len(source.text)),)
    spans: list[tuple[int, int]] = []
    end = len(source.text)
    for sent in reversed(source.sources):
        start = end - len(sent.text)
        if start < 0 or source.text[start:end] != sent.text:
            return None
        marker = re.search(r"\[page=(\d+) [^\]\n]+\]\n$", source.text[:start])
        if marker is None or int(marker[1]) != sent.page:
            return None
        spans.append((start, end))
        end = marker.start() - 2
        if (
            spans
            and len(spans) < len(source.sources)
            and source.text[end : marker.start()] != "\n\n"
        ):
            return None
    return tuple(reversed(spans))


def sent_page_texts(source: ExtractionSource) -> Mapping[int, tuple[str, ...]]:
    """按构建时偏移保留同页连续正文；元数据、页标记和其他页不能接成引文。"""
    spans = source.spans if source.spans is not None else _legacy_spans(source)
    if spans is None or len(spans) != len(source.sources):
        return {}
    runs: dict[int, list[str]] = defaultdict(list)
    previous: tuple[int, int] | None = None
    for sent, (start, end) in zip(source.sources, spans, strict=True):
        if not (0 <= start < end <= len(source.text)) or source.text[start:end] != sent.text:
            return {}
        if previous is not None and start < previous[1]:
            return {}
        text = clean_untrusted(source.text[start:end])
        gap = "" if previous is None else source.text[previous[1] : start]
        if previous is not None and previous[0] == sent.page and not gap.strip():
            runs[sent.page][-1] += gap + text
        else:
            runs[sent.page].append(text)
        previous = sent.page, end
    return {page: tuple(items) for page, items in runs.items()}


def _normalized(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split()).casefold()


def _block_text(block: Block) -> str:
    return "\n".join(part for part in (block.caption, block.text) if part)


def _tokens(text: str) -> int:
    return max(estimate_tokens(text), (len(text.encode("utf-8")) + 1) // 2)


def _bounded_prefix(text: str, token_budget: int, *, prefix: str = "") -> str:
    """保留连续原文前缀；无空格标识符也必须受完整来源的保守预算限制。"""
    low, high = 0, min(len(text), max(0, token_budget) * 2)
    while low < high:
        middle = (low + high + 1) // 2
        if _tokens(prefix + text[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _source_blocks(
    document: NormalizedDocument, chunks: ChunkedDocument, token_budget: int
) -> Iterable[tuple[SentBlock, str]]:
    for chunk in select_chunks(chunks.chunks, token_budget):
        for piece in chunk.text.split("\n\n"):
            if not piece.strip():
                continue
            for block in document.blocks:
                if chunk.page_start <= block.page <= chunk.page_end and _normalized(
                    piece
                ) in _normalized(_block_text(block)):
                    yield SentBlock(block, piece), chunk.section_type


def extraction_source(
    paper: Paper, document: NormalizedDocument, chunks: ChunkedDocument, token_budget: int
) -> ExtractionSource:
    """沿用章节筛选，把已选片段定位到真实块后发送零基页码，避免多页块的页码歧义。"""
    header = _bounded_prefix(paper_header(paper), min(400, max(0, token_budget) // 4))
    sources: list[SentBlock] = []
    spans: list[tuple[int, int]] = []
    parts: list[str] = []
    for source, section in _source_blocks(document, chunks, token_budget):
        if any(item.page == source.page and item.text == source.text for item in sources):
            continue
        marker = f"[page={source.page} {section}]\n"
        prefix = "\n\n".join((header, *parts, marker))
        text = _bounded_prefix(source.text, token_budget, prefix=prefix)
        if text.strip():
            sources.append(SentBlock(source.block, text))
            spans.append((len(prefix), len(prefix) + len(text)))
            parts.append(marker + text)
    return ExtractionSource("\n\n".join((header, *parts)), tuple(sources), tuple(spans))


def _contains_term(name: str, text: str) -> bool:
    needle = _normalized(name)
    if not needle:
        return False
    left = r"(?<![a-z0-9_])" if needle[0].isascii() and needle[0].isalnum() else ""
    right = r"(?![a-z0-9_])" if needle[-1].isascii() and needle[-1].isalnum() else ""
    return re.search(left + re.escape(needle) + right, _normalized(text)) is not None


def _term_source(item: Mapping[str, Any], source: ExtractionSource) -> SentBlock | None:
    return next(
        (
            sent
            for sent in source.sources
            if sent.page == item["page"]
            and _contains_term(item["name"], clean_untrusted(sent.text))
            and _contains_term(item["name"], _block_text(sent.block))
        ),
        None,
    )


def _verified_definition(
    item: Mapping[str, Any], document: NormalizedDocument, source: ExtractionSource
) -> bool:
    definition = item["definition"]
    if definition is None or len(definition.strip()) < MIN_QUOTE_CHARS:
        return False
    page = item["page"]
    original = "\n".join(_block_text(b) for b in document.blocks if b.page == page)
    return (
        verify_quote(definition, original)
        and any(verify_quote(definition, run) for run in sent_page_texts(source).get(page, ()))
        and verify_quote(definition, clean_untrusted(source.text))
    )


def _checked_concepts(
    items: Iterable[Mapping[str, Any]],
    document: NormalizedDocument,
    source: ExtractionSource,
) -> tuple[list[dict[str, Any]], Iterable[tuple[dict[str, Any], SentBlock]]]:
    checked: list[dict[str, Any]] = []
    chosen: dict[str, tuple[dict[str, Any], SentBlock]] = {}
    for item in items:
        located = _term_source(item, source)
        verified = located is not None and _verified_definition(item, document, source)
        enriched = {**item, "verified": verified}
        checked.append(enriched)
        if located is not None:
            name = normalize_concept_name(item["name"])
            current = chosen.get(name)
            rank = (_ROLE_PRIORITY[item["role"]], verified)
            if current is None or rank > (
                _ROLE_PRIORITY[current[0]["role"]],
                current[0]["verified"],
            ):
                chosen[name] = enriched, located
    return checked, chosen.values()


def _bbox(item: Mapping[str, Any], located: SentBlock, document: NormalizedDocument) -> BBox | None:
    if not item["verified"]:
        return located.block.bbox
    return next(
        (
            block.bbox
            for block in document.blocks
            if block.page == item["page"] and verify_quote(item["definition"], _block_text(block))
        ),
        None,
    )


def ingest_concepts(
    ctx: AppContext,
    paper_id: str,
    data: Mapping[str, Any],
    document: NormalizedDocument,
    source: ExtractionSource,
    *,
    input_hash: str,
    producer_version: str,
    now: str,
) -> dict[str, Any]:
    """调用者须和 extraction 在同一事务中执行；历史无概念字段时不回填。"""
    if "key_concepts" not in data:
        return dict(data)
    checked, chosen = _checked_concepts(data["key_concepts"], document, source)
    occurrences: dict[str, ConceptOccurrence] = {}
    for item, located in chosen:
        concept = ctx.store.knowledge.register(item["name"], None, now)
        occurrence = ConceptOccurrence(
            concept.id,
            paper_id,
            cast(ConceptRole, item["role"]),
            item["definition"] if item["verified"] else None,
            item["page"],
            _bbox(item, located, document),
            item["verified"],
            input_hash,
            producer_version,
            now,
        )
        current = occurrences.get(concept.id)
        if current is None or (_ROLE_PRIORITY[occurrence.role], occurrence.verified) > (
            _ROLE_PRIORITY[current.role],
            current.verified,
        ):
            occurrences[concept.id] = occurrence
    for occurrence in occurrences.values():
        ctx.store.knowledge.add_occurrence(occurrence)
    return {**data, "key_concepts": checked}
