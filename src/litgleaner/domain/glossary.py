"""课题术语表（第 7.1 节）：翻译和释义必须使用其中的译名，内容哈希计入译文指纹。"""

import re
from collections.abc import Sequence
from functools import cached_property

from pydantic import Field, field_validator

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.hashing import stable_hash


class GlossaryTerm(FrozenModel):
    en: tuple[str, ...]  # 英文写法，可以有多个（缩写、连字符变体）
    zh: str = Field(min_length=1)
    definition: str | None = None  # 一句话释义
    source: str | None = None  # 出处

    @field_validator("en", mode="before")
    @classmethod
    def _as_tuple(cls, value: object) -> object:
        return (value,) if isinstance(value, str) else value

    @field_validator("en")
    @classmethod
    def _not_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(v.strip() for v in value if v.strip())
        if not cleaned or len(cleaned) != len(value):
            raise ValueError("术语的英文写法不能为空")
        return cleaned

    @cached_property
    def pattern(self) -> re.Pattern[str]:
        options = "|".join(re.escape(v) for v in sorted(self.en, key=len, reverse=True))
        return re.compile(rf"(?<![\w-])(?:{options})(?![\w-])", re.IGNORECASE)


class Glossary(FrozenModel):
    terms: tuple[GlossaryTerm, ...] = ()

    @property
    def glossary_hash(self) -> str:
        """只看内容，不看顺序。"""
        entries = sorted((sorted(t.en), t.zh, t.definition or "") for t in self.terms)
        return stable_hash(entries)

    def terms_in(self, text: str) -> tuple[GlossaryTerm, ...]:
        return tuple(term for term in self.terms if term.pattern.search(text))

    def merged(self, other: "Glossary") -> "Glossary":
        """合并两份术语表；同一个英文写法以先出现的译名为准。"""
        seen = {v.lower() for t in self.terms for v in t.en}
        extra = tuple(t for t in other.terms if not seen & {v.lower() for v in t.en})
        return Glossary(terms=(*self.terms, *extra))

    def prompt_text(self, text: str) -> str:
        """只把这段原文用到的术语放进提示词，节省 token。"""
        lines = [f"- {' / '.join(t.en)} → {t.zh}" for t in self.terms_in(text)]
        return "\n".join(lines) if lines else "（本段没有术语表中的术语）"


def check_translation(
    sources: Sequence[str], lines: Sequence[str], glossary: Glossary
) -> tuple[str, ...]:
    """译文校验（ADR 0004）：句数一致；原句出现的术语，译句必须用术语表译名。"""
    if len(lines) != len(sources):
        return (f"句数不一致：原文 {len(sources)} 句，译文 {len(lines)} 句",)
    problems = []
    for number, (source, line) in enumerate(zip(sources, lines, strict=True), start=1):
        for term in glossary.terms_in(source):
            if term.zh not in line:
                problems.append(f"第 {number} 句缺少术语表译名“{term.zh}”（{term.en[0]}）")
    return tuple(problems)
