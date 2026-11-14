"""标题和摘要的保守关键词过滤；只判断是否值得进入模型初筛。"""

import re
import unicodedata
from dataclasses import dataclass

from litgleaner.domain.topic import TopicQuery

KEYWORD_FILTER_VERSION = "keyword-filter-v2"
_ASCII_WORD = re.compile(r"[a-z0-9_]")
_PLAIN = re.compile(r"[a-z0-9]+(?:[ -][a-z0-9]+)*")
_STOPWORDS = frozenset({"a", "an", "and", "by", "for", "in", "of", "on", "the", "to", "with"})
_SHORT = 5
# 去掉常见英文后缀得到词干，用前缀匹配词形变化；只用于包含词，不用于排除词
_SUFFIXES = (
    "ability",
    "ibility",
    "ations",
    "ation",
    "ility",
    "ness",
    "ment",
    "ions",
    "ion",
    "ing",
    "ity",
    "ies",
    "es",
    "ed",
    "s",
)


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


def _word_pattern(word: str) -> str:
    """短词（缩写、graph 等）只允许复数变化；长词去掉常见后缀后按词干前缀匹配。"""
    if len(word) <= _SHORT:
        return rf"(?<![a-z0-9_]){re.escape(word)}(?:s|es)?(?![a-z0-9_])"
    stem = next(
        (
            word[: -len(end)]
            for end in _SUFFIXES
            if word.endswith(end) and len(word) - len(end) >= 5
        ),
        word,
    )
    return rf"(?<![a-z0-9_]){re.escape(stem)}"


def _matches_words(term: str, texts: tuple[str, str]) -> bool:
    """包含词宽松匹配：同一字段里出现全部实词即可，顺序和词形不限。

    第 7.4 节的本地过滤只拦完全不相关的论文，相关性由模型初筛判断。
    """
    tokens = [w for w in re.split(r"[ -]", term) if w]
    if any(len(w) == 1 for w in tokens):
        return False  # t-cell、p value 这类带单字母的词，交给原样精确匹配
    words = [w for w in tokens if w not in _STOPWORDS]
    if not words:
        return False
    patterns = [re.compile(_word_pattern(word)) for word in words]
    return any(all(p.search(text) for p in patterns) for text in texts)


def _includes(term: str, texts: tuple[str, str]) -> bool:
    if _PLAIN.fullmatch(term) and not (" " not in term and "-" not in term and len(term) <= _SHORT):
        return _matches_words(term, texts) or _matches(term, texts)
    return _matches(term, texts)  # 短缩写、中文和含符号的词（C++）仍按原样精确匹配


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
        if _includes(term, texts):
            return KeywordFilterResult(True, f"命中包含词：{raw.strip()[:200]}")
    return KeywordFilterResult(False, "标题和摘要未命中包含词或其同义词")
