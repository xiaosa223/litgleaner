"""把文档块划分成章节。

依据实测的 MinerU 输出：text_level 不表示嵌套，IEEE 的 Abstract 是普通段落，
所以层级靠编号推断，摘要靠段首模式识别。
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Literal

from litfactory.domain.document import Block, NormalizedDocument

SectionType = Literal[
    "abstract",
    "introduction",
    "method",
    "results",
    "discussion",
    "conclusion",
    "limitation",
    "references",
    "acknowledgment",
    "other",
]

_ROMAN = re.compile(r"^(?P<num>[IVX]+)[.\s]\s*\S")
_LETTER = re.compile(r"^(?P<num>[A-Z])[.\s]\s*\S")
_DECIMAL = re.compile(r"^(?P<num>\d+(?:\.\d+)*)\.?\s+\S")
_RUN_IN_ABSTRACT = re.compile(r"^\s*(abstract\s*[—–\-:.]|摘\s*要\s*[:：]?)", re.IGNORECASE)
_RUN_IN_KEYWORDS = re.compile(r"^\s*(index terms|keywords?|关键词)\s*[—–\-:：.]?", re.IGNORECASE)
_NUMBERING = re.compile(r"^(?:[IVX]+[.\s]|[A-Z][.\s]|\d+(?:\.\d+)*\.?\s)\s*")

_PATTERNS: tuple[tuple[SectionType, re.Pattern[str]], ...] = (
    ("abstract", re.compile(r"^(abstract|摘\s*要)\b", re.IGNORECASE)),
    ("references", re.compile(r"\b(references|bibliography)\b|参考文献", re.IGNORECASE)),
    ("acknowledgment", re.compile(r"acknowledg|致\s*谢", re.IGNORECASE)),
    ("conclusion", re.compile(r"\bconclu|\bsummary\b|结论|总结", re.IGNORECASE)),
    ("limitation", re.compile(r"\blimitation|threats to validity|不足|局限", re.IGNORECASE)),
    ("discussion", re.compile(r"\bdiscussion|讨论", re.IGNORECASE)),
    (
        "results",
        re.compile(
            r"\b(results?|experiments?|experimental|evaluation|case stud\w+|performance)\b"
            r"|实验|结果|评估|案例",
            re.IGNORECASE,
        ),
    ),
    ("introduction", re.compile(r"\b(introduction|background)\b|引言|绪论|背景", re.IGNORECASE)),
    (
        "method",
        re.compile(
            r"\b(methods?|methodology|approach|framework|models?|algorithms?|proposed|architecture)\b"
            r"|方法|模型|算法",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class Section:
    section_type: SectionType
    path: tuple[str, ...]
    blocks: tuple[Block, ...]


def classify_heading(heading: str) -> SectionType | None:
    bare = _NUMBERING.sub("", heading.strip())
    for section_type, pattern in _PATTERNS:
        if pattern.search(bare):
            return section_type
    return None


@dataclass(frozen=True)
class _Outline:
    """当前标题栈。每次推进都生成新对象。"""

    stack: tuple[tuple[str, SectionType | None], ...] = ()
    last_letter: str | None = None

    def advance(self, heading: str) -> "_Outline":
        depth, letter = self._depth(heading)
        base = self.stack[:depth]
        explicit = classify_heading(heading)
        inherited = base[-1][1] if base else None
        entry = (heading, explicit or inherited)
        return _Outline(stack=(*base, entry), last_letter=letter)

    def _depth(self, heading: str) -> tuple[int, str | None]:
        if match := _DECIMAL.match(heading):
            return match.group("num").count("."), None
        letter = _LETTER.match(heading)
        if letter and self._is_next_letter(letter.group("num")):
            return 1, letter.group("num")
        if _ROMAN.match(heading):
            return 0, None
        if letter:
            return 1, letter.group("num")
        return 0, None

    def _is_next_letter(self, letter: str) -> bool:
        """“I.” 既可能是罗马数字也可能是小节字母，要看前一个小节是不是 H。"""
        if letter not in "IVX":
            return True
        return self.last_letter is not None and ord(letter) == ord(self.last_letter) + 1


@dataclass(frozen=True)
class _Draft:
    section_type: SectionType | None
    path: tuple[str, ...]
    blocks: tuple[Block, ...] = ()

    def add(self, block: Block) -> "_Draft":
        return replace(self, blocks=(*self.blocks, block))


def _front_section(block: Block, current: _Draft) -> _Draft | None:
    """在第一个编号章节之前，识别嵌在段落里的摘要和关键词。"""
    if _RUN_IN_ABSTRACT.match(block.text):
        return _Draft("abstract", ("Abstract",))
    if _RUN_IN_KEYWORDS.match(block.text):
        return _Draft("other", ("Keywords",))
    return None if current.section_type == "abstract" or current.path else _Draft("other", ())


def _collect(blocks: Iterable[Block]) -> tuple[_Draft, ...]:
    drafts: list[_Draft] = []
    outline = _Outline()
    current = _Draft("other", ())
    for block in blocks:
        if block.kind == "heading":
            drafts.append(current)
            outline = outline.advance(block.text)
            current = _Draft(outline.stack[-1][1], tuple(h for h, _ in outline.stack))
            continue
        if not outline.stack and block.kind == "text":
            started = _front_section(block, current)
            if started and started.path != current.path:
                drafts.append(current)
                current = started
        current = current.add(block)
    drafts.append(current)
    return tuple(draft for draft in drafts if draft.blocks)


def _infer_methods(drafts: tuple[_Draft, ...]) -> tuple[Section, ...]:
    """引言和结果之间没有明确类型的章节，通常是方法。"""
    types = [draft.section_type for draft in drafts]
    start = types.index("introduction") if "introduction" in types else None
    ends = [i for i, kind in enumerate(types) if kind in {"results", "discussion", "conclusion"}]
    end = ends[0] if ends else None
    sections = []
    for index, draft in enumerate(drafts):
        between = start is not None and end is not None and start < index < end
        resolved: SectionType = draft.section_type or ("method" if between else "other")
        sections.append(Section(resolved, draft.path, draft.blocks))
    return tuple(sections)


def split_sections(document: NormalizedDocument) -> tuple[Section, ...]:
    return _infer_methods(_collect(document.blocks))
