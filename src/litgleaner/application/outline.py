"""综述骨架预览、确认与只读历史；用户确认的范围不会被后台扩张。"""

from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.outline_input import (
    OutlineInput,
    prepare_input,
    producer_version,
    read_sources,
    source_paper,
)
from litgleaner.application.survey_plan import require_topic
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.outline import TopicOutline
from litgleaner.ports.store import JobSpec


@dataclass(frozen=True)
class OutlinePreview:
    topic_id: str
    topic_hash: str
    plan_hash: str
    input_hash: str
    producer_version: str | None
    total_papers: int
    input_paper_ids: tuple[str, ...]
    omitted_paper_ids: tuple[str, ...]
    input_tokens: int
    token_budget: int
    estimated_usd: float | None
    cached_outline_id: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class OutlineStatus:
    job_key: str
    status: str
    result: TopicOutline | None
    cost_usd: float
    last_error: str | None = None


@dataclass(frozen=True)
class OutlineView:
    topic_id: str
    result: TopicOutline | None
    total_papers: int
    new_paper_ids: tuple[str, ...]
    ungrouped: tuple[str, ...]
    stale: bool
    stale_reasons: tuple[str, ...]


def _prepare(ctx: AppContext, topic_id: str) -> tuple[OutlinePreview, OutlineInput]:
    topic = require_topic(ctx, topic_id)
    prepared = prepare_input(ctx, topic)
    producer = producer_version(ctx)
    saved = (
        None
        if producer is None
        else ctx.store.topic_outlines.by_fingerprint(
            topic.id, topic.topic_hash, prepared.input_hash, producer
        )
    )
    plan_hash = stable_hash(
        {
            "topic": topic.id,
            "hash": topic.topic_hash,
            "input": prepared.input_hash,
            "producer": producer,
            "considered": prepared.considered_ids,
        }
    )
    cost = (
        0.0
        if saved is not None or not prepared.paper_ids
        else (ctx.store.runs.average_llm_cost("outline"))
    )
    return OutlinePreview(
        topic.id,
        topic.topic_hash,
        plan_hash,
        prepared.input_hash,
        producer,
        len(prepared.considered_ids),
        prepared.paper_ids,
        prepared.omitted_ids,
        prepared.tokens,
        ctx.settings.outline.token_budget,
        cost,
        saved.id if saved else None,
        prepared.warnings,
    ), prepared


def preview(ctx: AppContext, topic_id: str) -> OutlinePreview:
    return _prepare(ctx, topic_id)[0]


def queue(ctx: AppContext, topic_id: str, plan_hash: str) -> OutlineStatus:
    ctx.llm.require_route("outline")
    plan, prepared = _prepare(ctx, topic_id)
    if plan.plan_hash != plan_hash:
        raise InvalidRequest("综述输入或模型配置已变化，请重新预览后确认")
    if not prepared.paper_ids:
        raise InvalidRequest("没有可发送的相关已读论文，请检查阅读状态和综述输入预算")
    key = f"outline:{topic_id}:{plan.topic_hash}:{plan.input_hash}:{plan.producer_version}"
    payload = {
        "topic_id": topic_id,
        "topic_hash": plan.topic_hash,
        "input_hash": plan.input_hash,
        "producer_version": plan.producer_version,
        "inputs": prepared.text,
        "input_paper_ids": prepared.paper_ids,
        "source_hashes": prepared.source_hashes,
        "considered_paper_ids": prepared.considered_ids,
        "reading_language": ctx.settings.reading_language,
        "token_budget": plan.token_budget,
        "warnings": prepared.warnings,
        "scope_id": key,
        "goal_ids": tuple(goal.id for goal in ctx.topics[topic_id].goals),
        "papers": [paper.model_dump(mode="json") for paper in prepared.papers],
        "eligible_gap_ids": prepared.eligible_gap_ids,
    }
    ctx.store.jobs.enqueue(JobSpec(key, "outline", payload), ctx.now_iso())
    if plan.cached_outline_id is not None:
        ctx.store.jobs.mark_done(key, ctx.now_iso())
    return status(ctx, topic_id, key)


def status(ctx: AppContext, topic_id: str, job_key: str) -> OutlineStatus:
    job = ctx.store.jobs.get(job_key)
    if job is None or job.stage != "outline" or job.payload.get("topic_id") != topic_id:
        raise NotFound("综述骨架任务不存在")
    p = job.payload
    saved = ctx.store.topic_outlines.by_fingerprint(
        topic_id, p["topic_hash"], p["input_hash"], p["producer_version"]
    )
    scope = saved.scope_id if saved else p["scope_id"]
    return OutlineStatus(
        job_key, job.status, saved, ctx.store.runs.llm_cost_for_scope(scope), job.last_error
    )


def result(ctx: AppContext, topic_id: str, outline_id: str) -> TopicOutline:
    saved = ctx.store.topic_outlines.get(outline_id)
    if saved is None or saved.topic_id != topic_id:
        raise NotFound("综述骨架不存在")
    return saved


def view(ctx: AppContext, topic_id: str) -> OutlineView:
    topic = require_topic(ctx, topic_id)
    snapshot, papers = read_sources(ctx, topic)
    hashes = {paper.id: source_paper(ctx, topic, paper, snapshot)[1] for paper in papers}
    producer = producer_version(ctx)
    candidates = (
        ()
        if producer is None
        else ctx.store.topic_outlines.compatible(
            topic.id,
            topic.topic_hash,
            producer,
            ctx.settings.reading_language,
            ctx.settings.outline.token_budget,
        )
    )
    saved = next(
        (
            item
            for item in candidates
            if {id_: hashes[id_] for id_ in item.input_paper_ids if id_ in hashes}
            == item.source_hashes
        ),
        None,
    )
    saved = saved or (candidates[0] if candidates else ctx.store.topic_outlines.latest(topic.id))
    reasons = _stale_reasons(ctx, topic_id, saved, hashes)
    assigned = {id_ for group in saved.groups for id_ in group.paper_ids} if saved else set()
    considered = frozenset(saved.considered_paper_ids) if saved else frozenset()
    return OutlineView(
        topic_id,
        saved,
        len(papers),
        tuple(id_ for id_ in hashes if id_ not in considered),
        tuple(id_ for id_ in hashes if id_ not in assigned),
        bool(reasons),
        reasons,
    )


def _stale_reasons(
    ctx: AppContext, topic_id: str, saved: TopicOutline | None, hashes: dict[str, str]
) -> tuple[str, ...]:
    if saved is None:
        return ()
    reasons: list[str] = []
    if saved.topic_hash != ctx.topics[topic_id].topic_hash:
        reasons.append("研究画像已变化")
    if saved.reading_language != ctx.settings.reading_language:
        reasons.append("阅读语言已变化")
    if saved.token_budget != ctx.settings.outline.token_budget:
        reasons.append("综述输入预算已变化")
    producer = producer_version(ctx)
    if producer is not None and producer != saved.producer_version:
        reasons.append("综述模型或提示词已变化")
    if {id_: hashes[id_] for id_ in saved.input_paper_ids if id_ in hashes} != saved.source_hashes:
        reasons.append("已发送论文的提取、引用、评分、课题归属或已读状态已变化")
    return tuple(reasons)
