"""把主题关键词翻译成各家检索语法，或者在本地过滤不支持布尔检索的结果。"""

import unicodedata
from collections.abc import Callable

from litfactory.domain.query import KeywordSearch


def _quoted(term: str) -> str:
    return '"' + term.replace('"', " ").strip() + '"'


def boolean_query(
    query: KeywordSearch,
    *,
    phrase: Callable[[str], str] = _quoted,
    or_op: str = "OR",
    not_op: str = "NOT",
) -> str:
    """("a" OR "b") NOT ("c")。phrase 决定每个词的写法，例如 all:"a"、"a"[tiab]。"""
    included = f" {or_op} ".join(phrase(term) for term in query.terms)
    text = f"({included})"
    if query.exclude:
        excluded = f" {or_op} ".join(phrase(term) for term in query.exclude)
        text += f" {not_op} ({excluded})"
    return text


def _fold(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def matches_terms(query: KeywordSearch, title: str, abstract: str | None) -> bool:
    """至少命中一个关键词、且不含排除词。用于只按相关度排序、不支持布尔检索的 API。"""
    haystack = _fold(f"{title} {abstract or ''}")
    if any(_fold(term) in haystack for term in query.exclude):
        return False
    return any(_fold(term) in haystack for term in query.terms)
