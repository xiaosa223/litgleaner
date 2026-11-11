"""首发阅读语言与保守的同语言跳过规则，不作为通用语言识别器。"""

import re
from typing import Literal

LanguageCode = Literal["zh-CN", "en"]

_NAMES: dict[LanguageCode, str] = {"zh-CN": "简体中文", "en": "English"}
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\U00020000-\U0002fa1f]")
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]")
_WORDS = re.compile(r"[a-zA-Z]+(?:'[a-zA-Z]+)?")
_ENGLISH_FUNCTION_WORDS = frozenset(
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
        "both",
        "but",
        "by",
        "can",
        "could",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "must",
        "not",
        "of",
        "on",
        "only",
        "or",
        "our",
        "should",
        "such",
        "than",
        "that",
        "the",
        "their",
        "then",
        "these",
        "they",
        "this",
        "those",
        "to",
        "was",
        "we",
        "were",
        "when",
        "where",
        "which",
        "with",
        "without",
        "would",
    ]
)


def language_name(code: LanguageCode) -> str:
    return _NAMES[code]


def source_language(text: str) -> LanguageCode | None:
    """只跳过有明确中英文信号的正文；混合文字、短语和其他语言保留翻译入口。"""
    if _KANA.search(text):
        return None
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return None
    han = [char for char in letters if _HAN.fullmatch(char)]
    if han:
        return "zh-CN" if len(han) >= 2 and len(han) == len(letters) else None
    if any(not char.isascii() for char in letters):
        return None  # 带重音的拉丁文字、希腊字母等都不能按英文跳过
    words = _WORDS.findall(text.lower())
    hints = set(words) & _ENGLISH_FUNCTION_WORDS
    if len(words) >= 3 and (len(hints) >= 2 or (words[0] == "abstract" and "of" in hints)):
        return "en"
    return None


def needs_translation(text: str, target: LanguageCode) -> bool:
    return source_language(text) != target
