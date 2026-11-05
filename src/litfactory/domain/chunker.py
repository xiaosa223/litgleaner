"""切分器：纯函数，同样的文档和配置永远得到同样的结果。"""

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from litfactory.domain.document import Block, NormalizedDocument, Quality
from litfactory.domain.hashing import stable_hash
from litfactory.domain.sections import Section, SectionType, split_sections

ALGORITHM_VERSION: Final = 1
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿]")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+|(?<=[。！？])\s*(?=\S)")
_DROPPED: Final = frozenset({"references", "acknowledgment"})
_LATIN_TOKENS_PER_WORD: Final = 4 / 3
_FALLBACK_SECTIONS: Final = 2
_STANDALONE: Final = frozenset({"table", "figure", "code"})


def estimate_tokens(text: str) -> int:
    """估算值，不依赖具体模型的分词器。用于切分和预算，不用于计费。"""
    cjk = len(_CJK.findall(text))
    words = len(_CJK.sub(" ", text).split())
    return cjk + math.ceil(words * _LATIN_TOKENS_PER_WORD)


@dataclass(frozen=True)
class ChunkerConfig:
    target_tokens: int = 800
    max_tokens: int = 1200
    overlap_sentences: int = 1

    @property
    def version(self) -> str:
        settings = (self.target_tokens, self.max_tokens, self.overlap_sentences)
        return f"c{ALGORITHM_VERSION}-{stable_hash(settings)[:8]}"


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    section_type: SectionType
    section_path: tuple[str, ...]
    page_start: int
    page_end: int
    token_count: int
    text: str


@dataclass(frozen=True)
class ChunkedDocument:
    chunker_version: str
    chunks: tuple[Chunk, ...]
    references: tuple[str, ...]
    quality: Quality
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Unit:
    text: str
    page: int
    standalone: bool = False

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


def _hard_split(text: str, limit: int) -> tuple[str, ...]:
    """没有句子边界的超长文本，按词（中文按字）切开。"""
    tokens = re.findall(r"[぀-ヿ㐀-鿿豈-﫿]|\S+\s*", text)
    pieces: list[str] = []
    current = ""
    for token in tokens:
        if current and estimate_tokens(current + token) > limit:
            pieces.append(current)
            current = ""
        current += token
    return (*pieces, current) if current else tuple(pieces)


def _sentences(text: str) -> tuple[str, ...]:
    """切句时保留原有分隔符，重新拼接后和原文完全一致。"""
    pieces: list[str] = []
    start = 0
    for match in _SENTENCE_END.finditer(text):
        if match.end() > start:
            pieces.append(text[start : match.end()])
            start = match.end()
    return (*pieces, text[start:]) if start < len(text) else tuple(pieces)


def _split_paragraph(text: str, config: ChunkerConfig) -> tuple[str, ...]:
    if estimate_tokens(text) <= config.max_tokens:
        return (text,)
    sentences = [
        piece
        for sentence in _sentences(text)
        for piece in _hard_split(sentence, config.target_tokens)
        if piece.strip()
    ]
    pieces: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        if current and estimate_tokens("".join((*current, sentence))) > config.target_tokens:
            pieces.append("".join(current).strip())
            current = _overlap(current, sentence, config)
        current = [*current, sentence]
    return (*pieces, "".join(current).strip()) if current else tuple(pieces)


def _overlap(previous: list[str], next_sentence: str, config: ChunkerConfig) -> list[str]:
    """重叠只在长段落被迫切开时发生，放不下就放弃，不能突破上限。"""
    count = config.overlap_sentences
    tail = previous[-count:] if count else []
    fits = estimate_tokens("".join((*tail, next_sentence))) <= config.target_tokens
    return tail if fits else []


def _block_units(block: Block, config: ChunkerConfig) -> tuple[_Unit, ...]:
    if block.kind in _STANDALONE:
        text = "\n".join(part for part in (block.caption, block.text) if part)
        return (_Unit(text, block.page, standalone=True),) if text.strip() else ()
    if block.kind == "equation":
        return (_Unit(block.text, block.page),) if block.text.strip() else ()
    pieces = _split_paragraph(block.text, config) if block.text.strip() else ()
    return tuple(_Unit(piece, block.page) for piece in pieces)


def _pack(units: Iterable[_Unit], target: int) -> tuple[tuple[_Unit, ...], ...]:
    groups: list[tuple[_Unit, ...]] = []
    current: tuple[_Unit, ...] = ()
    for unit in units:
        if unit.standalone:
            groups.extend(group for group in (current, (unit,)) if group)
            current = ()
            continue
        size = estimate_tokens(" ".join(item.text for item in (*current, unit)))
        if current and size > target:
            groups.append(current)
            current = ()
        current = (*current, unit)
    return (*groups, current) if current else tuple(groups)


def _with_conclusion_fallback(
    sections: tuple[Section, ...],
) -> tuple[tuple[Section, ...], tuple[str, ...]]:
    if any(section.section_type == "conclusion" for section in sections):
        return sections, ()
    body = [
        index
        for index, section in enumerate(sections)
        if section.section_type not in _DROPPED and section.path
    ]
    chosen = set(body[-_FALLBACK_SECTIONS:])
    updated = tuple(
        Section("conclusion", section.path, section.blocks) if index in chosen else section
        for index, section in enumerate(sections)
    )
    return updated, ("未找到结论章节，已用最后两节代替",)


def _build_chunks(
    sections: Iterable[Section], doc_sha: str, config: ChunkerConfig
) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for section in sections:
        units = [unit for block in section.blocks for unit in _block_units(block, config)]
        for group in _pack(units, config.target_tokens):
            joined = "\n\n".join(unit.text for unit in group)
            chunks.append(
                Chunk(
                    chunk_id=f"{doc_sha[:16]}:{config.version}:{len(chunks)}",
                    section_type=section.section_type,
                    section_path=section.path,
                    page_start=min(unit.page for unit in group),
                    page_end=max(unit.page for unit in group),
                    token_count=estimate_tokens(joined),
                    text=joined,
                )
            )
    return tuple(chunks)


def chunk_document(document: NormalizedDocument, config: ChunkerConfig) -> ChunkedDocument:
    sections, warnings = _with_conclusion_fallback(split_sections(document))
    references = tuple(
        block.text
        for section in sections
        for block in section.blocks
        if block.kind == "reference" or section.section_type == "references"
    )
    body = [
        Section(
            section.section_type,
            section.path,
            tuple(block for block in section.blocks if block.kind != "reference"),
        )
        for section in sections
        if section.section_type not in _DROPPED
    ]
    degraded = document.quality == "degraded" or bool(warnings)
    return ChunkedDocument(
        chunker_version=config.version,
        chunks=_build_chunks(body, document.source_pdf_sha256, config),
        references=references,
        quality="degraded" if degraded else "ok",
        warnings=warnings,
    )
