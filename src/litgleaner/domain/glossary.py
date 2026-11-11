"""课题术语表（第 7.1 节）：翻译和释义必须使用其中的译名，内容哈希计入译文指纹。"""

import re
from collections.abc import Sequence
from functools import cached_property

from pydantic import AliasChoices, Field, field_validator

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import LanguageCode


class GlossaryTerm(FrozenModel):
    forms: tuple[str, ...] = Field(validation_alias=AliasChoices("forms", "en"))
    translation: str = Field(min_length=1, validation_alias=AliasChoices("translation", "zh"))
    definition: str | None = None  # 一句话释义
    source: str | None = None  # 出处

    @field_validator("forms", mode="before")
    @classmethod
    def _as_tuple(cls, value: object) -> object:
        return (value,) if isinstance(value, str) else value

    @field_validator("forms")
    @classmethod
    def _not_blank(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(v.strip() for v in value if v.strip())
        if not cleaned or len(cleaned) != len(value):
            raise ValueError("术语的原文写法不能为空")
        return cleaned

    @cached_property
    def pattern(self) -> re.Pattern[str]:
        options = []
        for form in sorted(self.forms, key=len, reverse=True):
            escaped = re.escape(form)
            if not re.search(r"[㐀-鿿\U00020000-\U0002fa1f]", form):
                escaped = rf"(?<![\w-]){escaped}(?![\w-])"
            options.append(escaped)
        return re.compile("|".join(options), re.IGNORECASE)

    @property
    def en(self) -> tuple[str, ...]:
        """旧配置与调用方兼容；新配置使用 forms。"""
        return self.forms

    @property
    def zh(self) -> str:
        """旧配置与调用方兼容；新配置使用 translation。"""
        return self.translation


class Glossary(FrozenModel):
    target_lang: LanguageCode = "zh-CN"
    terms: tuple[GlossaryTerm, ...] = ()

    @property
    def glossary_hash(self) -> str:
        """只看内容，不看顺序。"""
        entries = sorted((sorted(t.forms), t.translation, t.definition or "") for t in self.terms)
        return stable_hash({"target_lang": self.target_lang, "terms": entries})

    def terms_in(self, text: str) -> tuple[GlossaryTerm, ...]:
        return tuple(term for term in self.terms if term.pattern.search(text))

    def merged(self, other: "Glossary") -> "Glossary":
        """合并同目标语言的术语表；同一写法以先出现的译名为准。"""
        if self.target_lang != other.target_lang:
            raise ValueError("不能合并不同目标语言的术语表")
        seen = {v.lower() for t in self.terms for v in t.forms}
        extra = tuple(t for t in other.terms if not seen & {v.lower() for v in t.forms})
        return Glossary(target_lang=self.target_lang, terms=(*self.terms, *extra))

    def prompt_text(self, text: str) -> str:
        """只把这段原文用到的术语放进提示词，节省 token。"""
        lines = [f"- {' / '.join(t.forms)} → {t.translation}" for t in self.terms_in(text)]
        empty = (
            "No glossary terms occur in this passage."
            if self.target_lang == "en"
            else "（本段没有术语表中的术语）"
        )
        return "\n".join(lines) if lines else empty


def check_translation(
    sources: Sequence[str], lines: Sequence[str], glossary: Glossary
) -> tuple[str, ...]:
    """译文校验（ADR 0004）：句数一致；原句出现的术语，译句必须用术语表译名。"""
    if len(lines) != len(sources):
        return (f"句数不一致：原文 {len(sources)} 句，译文 {len(lines)} 句",)
    problems = []
    for number, (source, line) in enumerate(zip(sources, lines, strict=True), start=1):
        for term in glossary.terms_in(source):
            if term.translation not in line:
                problems.append(
                    f"第 {number} 句缺少术语表译名“{term.translation}”（{term.forms[0]}）"
                )
    return tuple(problems)
