"""搜索副本统一字形；展示和来源核验始终保留真实原文。"""

import unicodedata
from collections.abc import Iterator

SEARCH_TEXT_VERSION = "memory-search-text-v1"
_EXCERPT_CHARS = 320
_CONTEXT_CHARS = 60


def normalize_search_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _ordered_characters(content: str) -> Iterator[tuple[str, int]]:
    """兼容分解后跨原字符稳定排序，原位置随字符保留。"""
    segment: list[tuple[str, int]] = []
    for position, original in enumerate(content):
        for value in unicodedata.normalize("NFKD", original):
            if not unicodedata.combining(value) and segment:
                yield from sorted(segment, key=lambda item: unicodedata.combining(item[0]))
                segment = []
            segment.append((value, position))
    yield from sorted(segment, key=lambda item: unicodedata.combining(item[0]))


def _composed_characters(content: str) -> list[tuple[str, int]]:
    """按规范组合的阻挡规则重组，包括组合类为零的 Hangul 字母。"""
    result: list[tuple[str, int]] = []
    starter: int | None = None
    last_class = 0
    for value, position in _ordered_characters(content):
        combining_class = unicodedata.combining(value)
        if starter is not None and (last_class < combining_class or last_class == 0):
            previous, origin = result[starter]
            combined = unicodedata.normalize("NFC", previous + value)
            if len(combined) == 1:
                result[starter] = combined, min(origin, position)
                continue
        if combining_class == 0:
            starter = len(result)
        result.append((value, position))
        last_class = combining_class
    return result


def _mapped_text(content: str) -> tuple[str, list[int]]:
    """与整体搜索协议相同的文字及原位置；casefold 与压空白各执行一次。"""
    values: list[str] = []
    origins: list[int] = []
    pending_space: int | None = None
    for value, position in _composed_characters(content):
        for folded in value.casefold():
            if folded.isspace():
                if values and pending_space is None:
                    pending_space = position
                continue
            if pending_space is not None:
                values.append(" ")
                origins.append(pending_space)
                pending_space = None
            values.append(folded)
            origins.append(position)
    return "".join(values), origins


def search_excerpt(content: str, terms: list[str]) -> str:
    normalized, origins = _mapped_text(content)
    positions = [at for term in terms if (at := normalized.find(term)) >= 0]
    raw_position = origins[min(positions)] if positions else 0
    start = max(0, raw_position - _CONTEXT_CHARS)
    end = start + _EXCERPT_CHARS
    return ("…" if start else "") + content[start:end] + ("…" if end < len(content) else "")
