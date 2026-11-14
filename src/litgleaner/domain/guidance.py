"""个性化导读、引导问题与回答评价（第 12.7 节）。引用一律是已核验的作者原文，页码零基。"""

from typing import Annotated, Literal

from pydantic import Field

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.mastery import Overall, QuestionKind, Verdict, evaluation_level

Treatment = Literal["full", "remind", "brief"]
GUIDE_SCHEMA_VERSION = "guide-v2"
EVALUATE_SCHEMA_VERSION = "evaluate-v1"
QUESTION_KINDS: tuple[QuestionKind, ...] = ("understand", "compare", "transfer")

Text = Annotated[str, Field(min_length=1, max_length=2000)]


class Quote(FrozenModel):
    page: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=2000)


class Sourced(FrozenModel):
    """一段模型文字及其已核验的原文；没有核验通过的引用时 inference 为真。"""

    text: Text
    citations: tuple[Quote, ...] = ()
    inference: bool = False


class Analogy(FrozenModel):
    concept_id: str | None = None  # 用户知识库里的概念；None 表示常识类比
    name: str = Field(min_length=1, max_length=200)
    text: Text
    breaks: Text  # 类比在哪里失效


class GuideConcept(FrozenModel):
    concept_id: str
    name: str
    level: int = Field(ge=0, le=3)  # 生成时的掌握程度
    treatment: Treatment
    explanation: Sourced
    analogy: Analogy | None = None
    anchor: str | None = None  # 用户自己写过的话，作锚点


class GuideQuestion(FrozenModel):
    index: int = Field(ge=0, le=2)
    kind: QuestionKind
    text: Text
    concept_ids: tuple[str, ...] = ()
    section: str | None = None  # 挂在哪个章节开头
    goal_id: str | None = None  # 迁移题对应的研究目标


class SectionHint(FrozenModel):
    section: str = Field(min_length=1, max_length=300)
    why: Text


class Guide(FrozenModel):
    id: str
    paper_id: str
    fingerprint: str
    producer_version: str
    reading_language: str
    created_at: str
    model: str
    knowledge_empty: bool
    overview: Sourced
    concepts: tuple[GuideConcept, ...]
    questions: tuple[GuideQuestion, ...] = Field(min_length=3, max_length=3)
    sections: tuple[SectionHint, ...] = ()
    scope_id: str = ""


class ConceptVerdict(FrozenModel):
    concept_id: str
    verdict: Verdict
    comment: Text
    citations: tuple[Quote, ...] = ()


class Evaluation(FrozenModel):
    id: str
    card_id: str
    overall: Overall
    verdicts: tuple[ConceptVerdict, ...]
    reference: Sourced
    advice: str = ""
    model: str
    prompt_version: str
    input_hash: str
    created_at: str


class AnswerCard(FrozenModel):
    id: str
    paper_id: str
    explanation_id: str
    question_index: int = Field(ge=0, le=2)
    revision: int = Field(ge=1)
    text: str = Field(min_length=1, max_length=2000)
    created_at: str


def treatment_for(level: int) -> Treatment:
    """没见过完整讲解；见过一句提醒加对比；能解释及以上一句带过。"""
    return "full" if level <= 0 else "remind" if level == 1 else "brief"


__all__ = ["Overall", "QuestionKind", "Verdict", "evaluation_level"]
