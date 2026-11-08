"""翻译和对照显示用的分句（纯函数）。

译文按句与原文对齐（ADR 0004），所以这里比切分器的分句更谨慎：缩写、作者名首字母、小数、
行内公式里的句点都不切开。切分器有自己的分句规则，改那边会让切分版本变化，所以不共用。
"""

import re

# 句点后面常常不是句子结束的缩写（小写比较）
_ABBREVIATIONS = frozenset(
    {
        "e.g", "i.e", "et al", "al", "etc", "vs", "cf", "fig", "figs", "eq", "eqs", "sec",
        "secs", "tab", "ref", "refs", "no", "nos", "approx", "resp", "dr", "mr", "mrs", "ms",
        "prof", "st", "vol", "pp", "ch", "app", "alg", "def", "thm", "lem", "prop",
    }
)  # fmt: skip
_CJK = re.compile(r"[㐀-鿿豈-﫿]")
_MATH = re.compile(r"\$\$.+?\$\$|\$(?=\S)[^$\n]+(?<=\S)\$(?!\d)", re.DOTALL)  # 金额不算公式
_END = re.compile(r"[.!?。！？]")
_CLOSERS = "\"'”’)]）】"


def _protected(text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _MATH.finditer(text)]


def _inside(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def _word_before(text: str, dot: int) -> str:
    match = re.search(r"([A-Za-z]+(?:\.[A-Za-z]+)*(?:\s+al)?)$", text[:dot])
    return match.group(1).lower() if match else ""


def _period_ends_sentence(text: str, dot: int, nxt: str) -> bool:
    """英文句点：后面要像新句子的开头，前面不能是缩写或作者名首字母（如 J. Smith）。"""
    if _CJK.match(nxt):
        return True  # 中文期刊常用半角句点加空格
    if not (nxt.isupper() or nxt.isdigit() or nxt in "\"'“‘([$"):
        return False
    word = _word_before(text, dot)
    return word not in _ABBREVIATIONS and not (len(word) == 1 and word.isalpha())


def _is_boundary(text: str, end: int) -> bool:
    """end 是句末标点之后（含紧随的引号、括号）的位置。"""
    mark = text[end - 1]
    rest = text[end:]
    following = rest.lstrip()
    if not following:
        return False  # 到结尾了，由调用方收尾
    if mark in "。！？":
        return True
    if len(following) == len(rest):
        return False  # 标点后面没有空白：小数、网址、缩写中间
    return mark in "!?" or _period_ends_sentence(text, end - 1, following[0])


def split_sentences(text: str) -> list[str]:
    spans = _protected(text)
    sentences: list[str] = []
    start = 0
    for match in _END.finditer(text):
        end = match.end()
        if _inside(match.start(), spans):
            continue
        while end < len(text) and text[end] in _CLOSERS:
            end += 1
        if _is_boundary(text, end):
            sentences.append(text[start:end])
            start = end
    sentences.append(text[start:])
    return [s.strip() for s in sentences if s.strip()]
