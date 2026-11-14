"""个性化导读与引导问题（第 12.7 节）：只读预览 → 用户确认 → 按需阶段调用一次模型 → 核验引用后缓存。

知识库变化后旧导读不自动重新生成，只在界面上提示可以重新生成。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from ulid import ULID

from litgleaner.application import concept_links
from litgleaner.application.context import AppContext
from litgleaner.application.guide_input import TASK, GuideInput, document, prepare, producer_version
from litgleaner.application.guide_schema import GUIDE_SCHEMA
from litgleaner.application.llm_gateway import LLMOutputInvalid
from litgleaner.application.qa_context import page_text
from litgleaner.application.stages.base import Stage
from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.errors import (
    BudgetExceeded,
    InvalidRequest,
    PermanentError,
    TaskBudgetExceeded,
)
from litgleaner.domain.evidence import verify_quote
from litgleaner.domain.guidance import (
    QUESTION_KINDS,
    Analogy,
    Guide,
    GuideConcept,
    GuideQuestion,
    Quote,
    SectionHint,
    Sourced,
    treatment_for,
)
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.ports.store import JobSpec

JobState = Literal[
    "none", "queued", "retrying", "failed", "configuration_required", "budget_exceeded"
]


@dataclass(frozen=True)
class PlanConcept:
    """生成前可以先标“我已经懂”的概念，作为用户自己定的掌握程度。"""

    concept_id: str
    name: str
    level: int


@dataclass(frozen=True)
class GuidePlan:
    configured: bool
    plan_hash: str | None
    estimated_usd: float | None
    knowledge_empty: bool
    concepts: tuple[PlanConcept, ...] = ()


def _plan_concepts(prepared: GuideInput) -> tuple[PlanConcept, ...]:
    return tuple(
        PlanConcept(row["concept_id"], row["name"], row["level"])
        for row in prepared.data["concepts"]
    )


@dataclass(frozen=True)
class GuideView:
    paper_id: str
    guide: Guide | None
    stale: bool  # 知识库、原文、画像或模型变了，可以重新生成
    plan: GuidePlan | None
    job: JobState
    unavailable: str | None = None


def _plan_hash(prepared: GuideInput, producer: str) -> str:
    return stable_hash({"fingerprint": prepared.fingerprint, "producer": producer})


def job_key(paper_id: str, prepared: GuideInput, producer: str) -> str:
    return f"{TASK}:{paper_id}:{prepared.fingerprint}:{producer}"


def job_state(ctx: AppContext, key: str | None, task: str) -> JobState:
    job = ctx.store.jobs.get(key) if key else None
    if job is None:
        return "none"
    if job.status in {"dead", "done"}:
        return "failed" if job.status == "dead" else "none"
    if task not in ctx.llm.routes:
        return "configuration_required"
    try:
        ctx.llm.check_budget(task)
    except (BudgetExceeded, TaskBudgetExceeded):
        return "budget_exceeded"
    return "retrying" if job.attempts else "queued"


def view(ctx: AppContext, paper_id: str) -> GuideView:
    latest = ctx.store.learning.latest_guide(paper_id)
    try:
        prepared = prepare(ctx, paper_id)
    except InvalidRequest as error:
        return GuideView(paper_id, latest, False, None, "none", str(error))
    producer = producer_version(ctx)
    if producer is None:
        plan = GuidePlan(False, None, None, prepared.knowledge_empty, _plan_concepts(prepared))
        return GuideView(paper_id, latest, latest is not None, plan, "none")
    current = ctx.store.learning.guide_by_fingerprint(paper_id, prepared.fingerprint, producer)
    plan = GuidePlan(
        True,
        _plan_hash(prepared, producer),
        ctx.store.runs.average_llm_cost(TASK),
        prepared.knowledge_empty,
        _plan_concepts(prepared),
    )
    state = job_state(ctx, job_key(paper_id, prepared, producer), TASK)
    return GuideView(
        paper_id, current or latest, current is None and latest is not None, plan, state
    )


def queue(ctx: AppContext, paper_id: str, plan_hash: str) -> GuideView:
    ctx.llm.require_route(TASK)
    prepared = prepare(ctx, paper_id)
    producer = producer_version(ctx)
    if producer is None or _plan_hash(prepared, producer) != plan_hash:
        raise InvalidRequest("知识库、原文或模型配置刚刚变了，请重新确认生成导读")
    if ctx.store.learning.guide_by_fingerprint(paper_id, prepared.fingerprint, producer) is None:
        key = job_key(paper_id, prepared, producer)
        payload = {
            "paper_id": paper_id,
            "fingerprint": prepared.fingerprint,
            "producer_version": producer,
            "reading_language": ctx.settings.reading_language,
            "inputs": prepared.text,
            "concept_ids": list(prepared.concept_ids),
            "known_ids": list(prepared.known_ids),
            "goal_ids": list(prepared.goal_ids),
            "knowledge_empty": prepared.knowledge_empty,
            "concepts": [dict(row) for row in prepared.data["concepts"]],
            "scope_id": key,
        }
        enqueue_or_retry(ctx, JobSpec(key, TASK, payload))
    return view(ctx, paper_id)


def enqueue_or_retry(ctx: AppContext, spec: JobSpec) -> None:
    """用户再次确认时，彻底失败过的同一任务重新排队；排队中、已完成的不动，不重复收费。"""
    if not ctx.store.jobs.enqueue(spec, ctx.now_iso()):
        existing = ctx.store.jobs.get(spec.key)
        if existing is not None and existing.status == "dead":
            ctx.store.jobs.reopen(spec.key, ctx.now_iso())


def verified_quotes(
    doc: NormalizedDocument, items: Iterable[Mapping[str, Any]]
) -> tuple[Quote, ...]:
    result: list[Quote] = []
    for item in items:
        quote = Quote(page=item["page"], quote=item["quote"])
        if verify_quote(quote.quote, page_text(doc, quote.page)) and quote not in result:
            result.append(quote)
    return tuple(result)


def sourced(doc: NormalizedDocument, raw: Mapping[str, Any]) -> Sourced:
    citations = verified_quotes(doc, raw.get("citations", ()))
    return Sourced(text=raw["text"], citations=citations, inference=not citations)


def _concepts(
    doc: NormalizedDocument, payload: Mapping[str, Any], raw: Iterable[Mapping[str, Any]]
) -> tuple[GuideConcept, ...]:
    by_id = {row["concept_id"]: row for row in payload["concepts"]}
    known = set(payload["known_ids"])
    seen: set[str] = set()
    result: list[GuideConcept] = []
    for item in raw:
        row = by_id.get(item["concept_id"])
        if row is None or row["concept_id"] in seen:
            continue
        seen.add(row["concept_id"])
        analogy = item.get("analogy")
        result.append(
            GuideConcept(
                concept_id=row["concept_id"],
                name=row["name"],
                level=row["level"],
                treatment=treatment_for(row["level"]),
                explanation=sourced(doc, item["explanation"]),
                analogy=_analogy(analogy, known) if analogy else None,
                anchor=(row["user_words"] or [None])[0] if row["level"] >= 1 else None,
            )
        )
    order = list(by_id)
    return tuple(sorted(result, key=lambda c: order.index(c.concept_id)))


def _analogy(raw: Mapping[str, Any], known: set[str]) -> Analogy:
    concept_id = raw.get("concept_id")
    return Analogy(
        concept_id=concept_id if concept_id in known else None,
        name=raw["name"],
        text=raw["text"],
        breaks=raw["breaks"],
    )


def _questions(
    payload: Mapping[str, Any], raw: list[Mapping[str, Any]]
) -> tuple[GuideQuestion, ...]:
    kinds = [item["kind"] for item in raw]
    if sorted(kinds) != sorted(QUESTION_KINDS):
        # 模型偶发失误：按输出不合格处理，有限重试
        raise LLMOutputInvalid("导读的三个问题必须分别是理解、对比、迁移题")
    allowed, goals = set(payload["concept_ids"]), set(payload["goal_ids"])
    ordered = sorted(raw, key=lambda item: QUESTION_KINDS.index(item["kind"]))
    return tuple(
        GuideQuestion(
            index=index,
            kind=item["kind"],
            text=item["text"],
            concept_ids=tuple(dict.fromkeys(c for c in item["concept_ids"] if c in allowed)),
            section=item.get("section"),
            goal_id=item.get("goal_id") if item.get("goal_id") in goals else None,
        )
        for index, item in enumerate(ordered)
    )


def build_guide(
    doc: NormalizedDocument,
    payload: Mapping[str, Any],
    data: Mapping[str, Any],
    *,
    model: str,
    now: str,
) -> Guide:
    return Guide(
        id=str(ULID()),
        paper_id=payload["paper_id"],
        fingerprint=payload["fingerprint"],
        producer_version=payload["producer_version"],
        reading_language=payload["reading_language"],
        created_at=now,
        model=model,
        knowledge_empty=bool(payload["knowledge_empty"]),
        overview=sourced(doc, data["overview"]),
        concepts=_concepts(doc, payload, data["concepts"]),
        questions=_questions(payload, list(data["questions"])),
        sections=tuple(SectionHint(section=s["section"], why=s["why"]) for s in data["sections"]),
        scope_id=payload["scope_id"],
    )


class GuideStage(Stage):
    name = TASK

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        p = payload
        if ctx.store.learning.guide_by_fingerprint(
            p["paper_id"], p["fingerprint"], p["producer_version"]
        ):
            return
        ctx.llm.require_route(TASK)
        if producer_version(ctx) != p["producer_version"]:
            raise PermanentError("导读模型或提示词已变化，请重新生成")
        doc = document(ctx, p["paper_id"])
        scoped = replace(ctx.llm, scope_id=p["scope_id"])
        variables = {"output_language": language_name(p["reading_language"])}
        result = scoped.call(TASK, variables, p["inputs"], GUIDE_SCHEMA, paper_id=p["paper_id"])
        guide = build_guide(doc, p, result.data, model=result.model, now=ctx.now_iso())
        with ctx.store.atomic():
            saved = ctx.store.learning.add_guide(guide)
            if saved.id == guide.id:
                concept_links.record_from_guide(ctx, guide, p, result.data)
