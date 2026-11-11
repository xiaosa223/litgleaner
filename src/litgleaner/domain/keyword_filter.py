"""标题和摘要的保守关键词过滤；只判断是否值得进入模型初筛。"""

import re
import unicodedata
from dataclasses import dataclass

from litgleaner.domain.topic import TopicQuery

KEYWORD_FILTER_VERSION = "keyword-filter-v1"
_ASCII_WORD = re.compile(r"[a-z0-9_]")


@dataclass(frozen=True)
class KeywordFilterResult:
    passed: bool
    reason: str


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _matches(term: str, texts: tuple[str, str]) -> bool:
    # ASCII 词不能命中别的单词内部；中文允许子串，英文缩写也可紧邻中文。
    before = r"(?<![a-z0-9_])" if term.isascii() or _ASCII_WORD.fullmatch(term[0]) else ""
    after = r"(?![a-z0-9_])" if term.isascii() or _ASCII_WORD.fullmatch(term[-1]) else ""
    pattern = re.compile(f"{before}{re.escape(term)}{after}")
    return any(pattern.search(text) is not None for text in texts)


def filter_keywords(title: str, abstract: str | None, query: TopicQuery) -> KeywordFilterResult:
    """使用 query.terms 已展开的同义词；排除优先，未配置包含词时交给模型。"""
    texts = (_normalize(title), _normalize(abstract or ""))
    for raw in query.exclude:
        term = _normalize(raw)
        if term and _matches(term, texts):
            return KeywordFilterResult(False, f"命中排除词：{raw.strip()[:200]}")
    terms = tuple((raw, _normalize(raw)) for raw in query.terms if _normalize(raw))
    if not terms:
        return KeywordFilterResult(True, "未配置包含词，交由模型初筛")
    for raw, term in terms:
        if _matches(term, texts):
            return KeywordFilterResult(True, f"命中包含词：{raw.strip()[:200]}")
    return KeywordFilterResult(False, "标题和摘要未命中包含词或其同义词")
