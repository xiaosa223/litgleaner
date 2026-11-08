"""翻译前的占位符保护（第 12.3 节）。

模型容易改写或丢掉的片段（行内公式、LaTeX 命令、网址、DOI、引用编号、作者-年份引用、
图表公式的交叉引用、Markdown 链接）先换成 ⟦n⟧，译后还原；占位符缺失、重复或编造都算校验失败。
匹配规则移植自 Agentero 的 mask.ts 和 preserved-links.ts（MIT，见 THIRD_PARTY.md）。
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

_AUTHOR = r"(?:[A-Z][A-Za-z'’-]{1,40}|[一-鿿]{1,12})"
_AUTHOR_TAIL = r"(?:\s*et\s+al\.?|\s*(?:and|&)\s+[A-Z][A-Za-z'’-]{1,40}|等人|等)?"
_AUTHOR_CITE = rf"{_AUTHOR}{_AUTHOR_TAIL}\s*[,，]\s*\d{{4}}[a-z]?(?:\s*年)?"
_REF_WORDS = (
    r"Figures?|Figs?\.|Tables?|Tabs?\.|Sections?|Secs?\.|Equations?|Eqs?\.|Eqn?\.|"
    r"Algorithms?|Algs?\.|Theorems?|Lemmas?|Appendices|Appendix"
)
_NUMBER = r"\(?\d+(?:\.\d+)?[a-z]?\)?"
_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\[\[\s*\d+\s*\]\]|⟦\s*\d+\s*⟧"),  # 原文里碰巧出现的编号和占位符，不能被当成协议
    re.compile(r"\[[^\]\n]{1,240}\]\(https?://[^\s<>\]]+\)"),  # Markdown 链接
    re.compile(r"https?://[^\s、，。<>\]]+"),
    re.compile(r"\bdoi:\s*10\.\d{4,9}/\S+", re.IGNORECASE),
    re.compile(r"\b10\.\d{4,9}/[^\s、，。]+"),
    re.compile(r"\$\$[^$]{1,400}\$\$"),
    re.compile(r"\$(?=\S)[^$\n]{1,200}(?<=\S)\$(?!\d)"),  # $5 and $10 是金额，不是公式
    re.compile(r"\\\((?:[^\\]|\\[^)]){1,200}?\\\)"),
    re.compile(r"\\[A-Za-z]+(?:\{[^{}]{0,120}\})?"),
    re.compile(r"[\[［]\s*\d+[a-z]?(?:\s*[-–—~,，]\s*\d+[a-z]?)*\s*[\]］]"),  # [12]、[1, 3]
    re.compile(rf"\b(?:{_REF_WORDS})\s+{_NUMBER}(?:\s*[-–—]\s*{_NUMBER})?", re.IGNORECASE),
    re.compile(rf"[(（]\s*{_AUTHOR_CITE}(?:\s*[;；]\s*{_AUTHOR_CITE})*\s*[)）]"),
)
_LINKISH = frozenset({1, 2, 3, 4})  # 末尾的句读不属于链接
_PLACEHOLDER = re.compile(r"⟦\s*(\d+)\s*⟧")


@dataclass(frozen=True)
class MaskToken:
    placeholder: str
    original: str


@dataclass(frozen=True)
class Masked:
    text: str
    tokens: tuple[MaskToken, ...]


@dataclass(frozen=True)
class Restored:
    text: str
    problems: tuple[str, ...]


def _paren_balance(text: str) -> int:
    return text.count("(") - text.count(")")


def _trim_link(raw: str) -> str:
    """去掉粘在链接末尾的句读；右括号只有多出来时才去掉，如 Function_(mathematics) 保留。"""
    end = len(raw)
    while end > 0:
        if raw[end - 1] in ".,;:!?" or (raw[end - 1] == ")" and _paren_balance(raw[:end]) < 0):
            end -= 1
        else:
            break
    return raw[:end]


def _is_batch_marker(text: str, start: int, end: int) -> bool:
    """[[n]] 是分批编号，不是引用。"""
    return text[start - 1 : start] == "[" and text[end : end + 1] == "]"


def _spans(text: str) -> list[tuple[int, int]]:
    found: list[tuple[int, int]] = []
    for number, pattern in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            start, end = match.span()
            if number in _LINKISH:
                end = start + len(_trim_link(match.group()))
            if end > start and not _is_batch_marker(text, start, end):
                found.append((start, end))
    found.sort(key=lambda span: (span[0], -span[1]))
    kept: list[tuple[int, int]] = []
    for start, end in found:
        if not kept or start >= kept[-1][1]:  # 重叠时先出现、更长的优先
            kept.append((start, end))
    return kept


def mask(text: str, start: int = 0) -> Masked:
    """按阅读顺序编号，从 start 开始；同一段落的多句接着编号，避免重号。"""
    tokens: list[MaskToken] = []
    pieces: list[str] = []
    cursor = 0
    for offset, (begin, end) in enumerate(_spans(text)):
        placeholder = f"⟦{start + offset}⟧"
        tokens.append(MaskToken(placeholder, text[begin:end]))
        pieces += [text[cursor:begin], placeholder]
        cursor = end
    pieces.append(text[cursor:])
    return Masked("".join(pieces), tuple(tokens))


def restore(text: str, tokens: Sequence[MaskToken]) -> Restored:
    """还原占位符。模型常把 ⟦0⟧ 写成 ⟦ 0 ⟧，按编号匹配。"""
    originals = {token.placeholder[1:-1]: token.original for token in tokens}
    seen = [match.group(1) for match in _PLACEHOLDER.finditer(text)]
    problems = [f"多出未知占位符 ⟦{n}⟧" for n in dict.fromkeys(seen) if n not in originals]
    for number in originals:
        count = seen.count(number)
        if count == 0:
            problems.append(f"缺少占位符 ⟦{number}⟧")
        elif count > 1:
            problems.append(f"占位符 ⟦{number}⟧ 重复")
    restored = _PLACEHOLDER.sub(lambda m: originals.get(m.group(1), m.group()), text)
    return Restored(restored, tuple(problems))
