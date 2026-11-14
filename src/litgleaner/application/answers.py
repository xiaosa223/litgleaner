"""回答卡片“先写后看”（第 12.7 节）：用户写的话只追加保存；提交后由 evaluate 评价并核验引用，
评价结果写成掌握事件。模型从不改写用户的回答。
"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_chunks
from litgleaner.application.guide import (
    JobState,
    enqueue_or_retry,
    job_state,
    sourced,
    verified_quotes,
)
from litgleaner.application.guide_input import document
from litgleaner.application.guide_schema import EVALUATE_SCHEMA
from litgleaner.application.llm_gateway import LLMOutputInvalid
from litgleaner.application.qa_context import select_context
from litgleaner.application.stages.base import Stage
from litgleaner.domain.errors import InvalidRequest, NotFound, PermanentError
from litgleaner.domain.guidance import (
    EVALUATE_SCHEMA_VERSION,
    AnswerCard,
    ConceptVerdict,
    Evaluation,
    Guide,
    GuideQuestion,
)
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.domain.mastery import MasteryEvent
from litgleaner.ports.feedback import FeedbackRecord
from litgleaner.ports.store import JobSpec

TASK = "evaluate"
SOURCE_TOKENS = 2000


@dataclass(frozen=True)
class CardView:
    question_index: int
    card: AnswerCard | None
    revisions: int
    evaluation: Evaluation | None
    evaluation_state: JobState


def producer_version(ctx: AppContext) -> str | None:
    route = ctx.llm.routes.get(TASK)
    if route is None:
        return None
    return stable_hash(
        {"model": route.model, "prompt": route.prompt.version, "schema": EVALUATE_SCHEMA_VERSION}
    )


def _job_key(card_id: str, producer: str) -> str:
    return f"{TASK}:{card_id}:{producer}"


def _guide(ctx: AppContext, paper_id: str, guide_id: str) -> Guide:
    guide = ctx.store.learning.guide(guide_id)
    if guide is None or guide.paper_id != paper_id:
        raise NotFound("导读不存在")
    return guide


def cards(ctx: AppContext, paper_id: str, guide_id: str) -> tuple[CardView, ...]:
    _guide(ctx, paper_id, guide_id)
    saved = ctx.store.learning.cards(guide_id)
    latest = {card.question_index: card for card in saved}  # 按修订号升序，最后的为最新
    evaluations = ctx.store.learning.evaluations(card.id for card in latest.values())
    producer = producer_version(ctx)
    views: list[CardView] = []
    for index in range(3):
        card = latest.get(index)
        evaluation = evaluations.get(card.id) if card else None
        key = _job_key(card.id, producer) if card and producer and not evaluation else None
        views.append(
            CardView(
                index,
                card,
                sum(1 for c in saved if c.question_index == index),
                evaluation,
                job_state(ctx, key, TASK) if key else "none",
            )
        )
    return tuple(views)


def submit(
    ctx: AppContext, paper_id: str, guide_id: str, question_index: int, text: str, *, evaluate: bool
) -> tuple[CardView, ...]:
    guide = _guide(ctx, paper_id, guide_id)
    if not 0 <= question_index < len(guide.questions):
        raise InvalidRequest("问题编号不存在")
    text = text.strip()
    if not text or len(text) > 2000:
        raise InvalidRequest("回答不能为空，且不超过 2000 字")
    if evaluate:  # 先确认能评价，失败时不留下半截修订
        ctx.llm.require_route(TASK)
        document(ctx, paper_id)
    latest = [c for c in ctx.store.learning.cards(guide_id) if c.question_index == question_index]
    if latest and latest[-1].text == text:
        card = latest[-1]  # 重复提交同样的文字：不新增修订，也不重复评价
    else:
        card = ctx.store.learning.add_card(
            AnswerCard(
                id=str(ULID()),
                paper_id=paper_id,
                explanation_id=guide_id,
                question_index=question_index,
                revision=1,
                text=text,
                created_at=ctx.now_iso(),
            )
        )
    if evaluate and ctx.store.learning.evaluation(card.id) is None:
        queue_evaluation(ctx, card, guide)
    return cards(ctx, paper_id, guide_id)


def _inputs(ctx: AppContext, card: AnswerCard, guide: Guide) -> Mapping[str, Any]:
    question = guide.questions[card.question_index]
    concepts = {c.concept_id: c for c in guide.concepts}
    rows = []
    for cid in question.concept_ids or tuple(concepts)[:3]:
        occurrence = next(
            (o for o in ctx.store.knowledge.for_paper(card.paper_id) if o.concept_id == cid), None
        )
        concept = ctx.store.knowledge.get(cid)
        if concept is not None:
            definition = occurrence.definition if occurrence and occurrence.verified else None
            rows.append({"concept_id": cid, "name": concept.name, "definition": definition})
    state = ctx.store.papers.get_fulltext(card.paper_id)
    if state is None or state.doc_key is None:
        raise InvalidRequest("这篇论文还没有解析出正文，无法评价")
    doc = document(ctx, card.paper_id)
    query = " ".join([question.text, card.text, *(str(r["name"]) for r in rows)])
    sources = select_context(
        doc, load_chunks(ctx, state.doc_key), query, token_budget=SOURCE_TOKENS
    )
    return {
        "question": {"kind": question.kind, "text": question.text, "goal_id": question.goal_id},
        "user_answer": card.text,
        "concepts": rows,
        "paper_title": (p.title if (p := ctx.store.papers.get(card.paper_id)) else ""),
        "paper_sources": sources.text,
    }


def queue_evaluation(ctx: AppContext, card: AnswerCard, guide: Guide) -> None:
    ctx.llm.require_route(TASK)
    producer = producer_version(ctx)
    assert producer is not None  # require_route 已检查
    data = _inputs(ctx, card, guide)
    key = _job_key(card.id, producer)
    payload = {
        "card_id": card.id,
        "guide_id": guide.id,
        "producer_version": producer,
        "reading_language": ctx.settings.reading_language,
        "inputs": json.dumps(data, ensure_ascii=False),
        "concept_ids": [row["concept_id"] for row in data["concepts"]],
        "scope_id": key,
    }
    enqueue_or_retry(ctx, JobSpec(key, TASK, payload))


def evaluate(ctx: AppContext, paper_id: str, card_id: str) -> tuple[CardView, ...]:
    card = ctx.store.learning.card(card_id)
    if card is None or card.paper_id != paper_id:
        raise NotFound("回答卡片不存在")
    guide = _guide(ctx, paper_id, card.explanation_id)
    if ctx.store.learning.evaluation(card_id) is None:
        queue_evaluation(ctx, card, guide)
    return cards(ctx, paper_id, guide.id)


def mark_wrong(ctx: AppContext, paper_id: str, card_id: str, reason: str) -> None:
    evaluation = ctx.store.learning.evaluation(card_id)
    card = ctx.store.learning.card(card_id)
    if evaluation is None or card is None or card.paper_id != paper_id:
        raise NotFound("评价不存在")
    record = FeedbackRecord(
        str(ULID()),
        paper_id,
        "reader",
        "evaluation_wrong",
        external_id=f"evaluation_wrong:{evaluation.id}",
        payload={"evaluation_id": evaluation.id, "card_id": card_id, "reason": reason[:500]},
    )
    ctx.store.feedback.add(record, ctx.now_iso())


def _verdicts(
    doc: Any, allowed: list[str], raw: Iterable[Mapping[str, Any]]
) -> tuple[ConceptVerdict, ...]:
    result: dict[str, ConceptVerdict] = {}
    for item in raw:
        if item["concept_id"] in allowed and item["concept_id"] not in result:
            result[item["concept_id"]] = ConceptVerdict(
                concept_id=item["concept_id"],
                verdict=item["verdict"],
                comment=item["comment"],
                citations=verified_quotes(doc, item["citations"]),
            )
    if set(result) != set(allowed):
        # 漏评的概念会让评价看起来完成、掌握程度却不更新：按输出不合格重试
        raise LLMOutputInvalid("评价没有覆盖题目考查的全部概念")
    return tuple(result[cid] for cid in allowed)


def _events(ctx: AppContext, evaluation: Evaluation, question: GuideQuestion) -> None:
    for verdict in evaluation.verdicts:
        ctx.store.learning.add_event(
            MasteryEvent(
                str(ULID()),
                verdict.concept_id,
                "evaluation",
                None,
                None,
                evaluation.id,
                evaluation.created_at,
                question.kind,
                verdict.verdict,
                evaluation.overall,
            )
        )


class EvaluateStage(Stage):
    name = TASK

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        p = payload
        if ctx.store.learning.evaluation(p["card_id"]) is not None:
            return
        card = ctx.store.learning.card(p["card_id"])
        guide = ctx.store.learning.guide(p["guide_id"])
        if card is None or guide is None:
            raise PermanentError("回答卡片或导读不存在")
        ctx.llm.require_route(TASK)
        if producer_version(ctx) != p["producer_version"]:
            raise PermanentError("评价模型或提示词已变化，请重新提交评价")
        doc = document(ctx, card.paper_id)
        scoped = replace(ctx.llm, scope_id=p["scope_id"])
        variables = {"output_language": language_name(p["reading_language"])}
        result = scoped.call(TASK, variables, p["inputs"], EVALUATE_SCHEMA, paper_id=card.paper_id)
        data = result.data
        evaluation = Evaluation(
            id=str(ULID()),
            card_id=card.id,
            overall=data["overall"],
            verdicts=_verdicts(doc, list(p["concept_ids"]), data["verdicts"]),
            reference=sourced(doc, data["reference"]),
            advice=data.get("advice", ""),
            model=result.model,
            prompt_version=result.prompt_version,
            input_hash=stable_hash(p["inputs"]),
            created_at=ctx.now_iso(),
        )
        with ctx.store.atomic():
            saved = ctx.store.learning.add_evaluation(evaluation)
            if saved.id == evaluation.id:
                _events(ctx, evaluation, guide.questions[card.question_index])
