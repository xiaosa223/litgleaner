"""检索生成可审查计划，确认后幂等排入调研任务。"""

from dataclasses import dataclass

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.application.survey_estimate import counts, estimates
from litgleaner.application.survey_plan import (
    assert_current,
    fingerprint,
    require_topic,
)
from litgleaner.application.survey_sources import collect
from litgleaner.application.topics import sync_topics
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.survey import SurveyPreview, SurveyRound
from litgleaner.ports.store import JobSpec


@dataclass(frozen=True)
class SurveyStatus:
    run_id: str
    status: str
    rounds: tuple[SurveyRound, ...]
    last_error: str | None = None


def preview(
    ctx: AppContext,
    topic_id: str,
    *,
    dois: tuple[str, ...] | None = None,
    historical: bool = True,
    plan_id: str | None = None,
    internal: bool = False,
) -> SurveyPreview:
    if plan_id is not None and (saved := ctx.store.surveys.preview(plan_id)) is not None:
        return saved
    topic = require_topic(ctx, topic_id)
    settings = ctx.settings.survey
    seeds = topic.seeds.dois if dois is None else dois
    result = collect(ctx, topic, settings, dois=seeds, historical=historical)
    planned = counts(ctx, topic, tuple(item.record for item in result.candidates))
    initial, pipeline = estimates(ctx, settings, planned)
    plan = SurveyPreview(
        id=plan_id or str(ULID()),
        topic_id=topic.id,
        topic_hash=topic.topic_hash,
        fingerprint=fingerprint(ctx, topic, settings),
        created_at=ctx.now_iso(),
        internal=internal,
        settings=settings,
        candidates=result.candidates,
        edges=result.edges,
        sources=result.sources,
        uncovered=result.uncovered,
        triage_calls=planned.triage_calls,
        fulltext_candidates=planned.fulltext_candidates,
        extract_calls=planned.extract_calls,
        assess_calls=planned.assess_calls,
        locally_rejected=planned.locally_rejected,
        known_papers=planned.known_papers,
        estimated_usd=initial,
        estimated_pipeline_usd=pipeline,
    )
    ctx.store.surveys.add_preview(plan)
    return plan


def get_preview(ctx: AppContext, topic_id: str, plan_id: str) -> SurveyPreview:
    plan = ctx.store.surveys.preview(plan_id)
    if plan is None or plan.topic_id != topic_id:
        raise NotFound("调研预览不存在")
    return plan


def queue(ctx: AppContext, topic_id: str, plan_id: str | None = None) -> SurveyStatus:
    if plan_id is None:
        saved = ctx.store.surveys.latest_preview(topic_id)
        if saved is None:
            raise InvalidRequest("请先预览调研范围和估算费用，再确认执行")
        plan_id = saved.id
    plan = get_preview(ctx, topic_id, plan_id)
    if plan.internal:
        raise InvalidRequest("该计划属于自动轮次，请确认用户保存的调研预览")
    if existing := ctx.store.surveys.accepted_run(plan_id):
        return status(ctx, topic_id, existing)
    assert_current(ctx, plan)
    sync_topics(ctx)
    if not plan.candidates:
        raise InvalidRequest("本次预览没有候选论文，请检查来源回执与未覆盖范围")
    if plan.triage_calls or plan.settings.rounds > 1:
        ctx.llm.require_route("triage")
    now, run_id = ctx.now_iso(), str(ULID())
    with ctx.store.atomic():
        if not ctx.store.surveys.accept(plan_id, run_id, now):
            existing = ctx.store.surveys.accepted_run(plan_id)
            if existing is None:
                raise RuntimeError("调研确认冲突后无法读取原任务")
            return status(ctx, topic_id, existing)
        ctx.store.jobs.enqueue(
            JobSpec(
                key=f"survey:{run_id}",
                stage="survey",
                payload={"run_id": run_id, "plan_id": plan_id},
            ),
            now,
        )
    return status(ctx, topic_id, run_id)


def status(ctx: AppContext, topic_id: str, run_id: str) -> SurveyStatus:
    job = ctx.store.jobs.get(f"survey:{run_id}")
    plan = ctx.store.surveys.preview(str(job.payload["plan_id"])) if job else None
    if job is None or plan is None or plan.topic_id != topic_id:
        raise NotFound("调研任务不存在")
    rounds = tuple(
        item
        for iteration in range(1, plan.settings.rounds + 1)
        if (item := ctx.store.surveys.round_for(run_id, iteration)) is not None
    )
    return SurveyStatus(run_id, job.status, rounds, job.last_error)
