"""从有限标题摘要生成候选检索词，不生成 FTS 运算符。"""

import re

from litgleaner.domain.search_text import normalize_search_text

TERMS_VERSION = "memory-candidate-terms-v1"
_INPUT_CHARS = 8192
_MAX_WORD_CHARS = 64
_WORDS = re.compile(r"\w+", re.UNICODE)
_CJK_CHARS = r"\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af\U00020000-\U0002fa1f"
_CJK = re.compile(f"[{_CJK_CHARS}]")
_SEGMENTS = re.compile(f"[{_CJK_CHARS}]+|[^{_CJK_CHARS}]+")
_STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "these",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "with",
    ]
)


def _parts(word: str) -> tuple[str, ...]:
    found: list[str] = []
    for segment in _SEGMENTS.findall(word):
        if _CJK.match(segment):
            found.extend(segment[index : index + 3] for index in range(len(segment) - 2))
        elif 3 <= len(segment) <= _MAX_WORD_CHARS and segment not in _STOP_WORDS:
            found.append(segment)
    return tuple(found)


def candidate_terms(title: str, abstract: str | None, limit: int = 32) -> tuple[str, ...]:
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("候选检索词数量必须为 1–64")
    if not isinstance(title, str) or (abstract is not None and not isinstance(abstract, str)):
        raise ValueError("候选检索只接受字符串标题与可选摘要")
    bounded = (title + "\n" + (abstract or ""))[:_INPUT_CHARS]
    normalized = normalize_search_text(bounded)
    found: dict[str, None] = {}
    for word in _WORDS.findall(normalized):
        for term in _parts(word):
            found.setdefault(term, None)
            if len(found) == limit:
                return tuple(found)
    return tuple(found)
