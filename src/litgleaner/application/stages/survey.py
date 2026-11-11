"""按已确认限额逐轮调研；结果首写冻结，预算或前置任务未就绪可恢复。"""

from collections.abc import Iterable, Mapping
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.stages.base import Stage
from litgleaner.application.survey import preview
from litgleaner.application.survey_ingest import prepare
from litgleaner.application.survey_jobs import cancel_children, run_children
from litgleaner.application.survey_plan import fingerprint
from litgleaner.application.survey_results import finish, seeds
from litgleaner.domain.errors import PermanentError, WaitingForWork
from litgleaner.domain.survey import SurveyPreview, SurveyWork
from litgleaner.ports.store import JobSpec


def _current(ctx: AppContext, plan: SurveyPreview) -> bool:
    topic = ctx.topics.get(plan.topic_id)
    return (
        topic is not None
        and plan.settings == ctx.settings.survey
        and (plan.fingerprint == fingerprint(ctx, topic, plan.settings))
    )


def _cancel(ctx: AppContext, original: SurveyPreview, run_id: str, iteration: int) -> None:
    work = ctx.store.surveys.get_work(run_id, iteration)
    if work is None:
        plan = original.model_copy(
            update={
                "id": f"survey-plan:{run_id}:{iteration}:cancelled",
                "internal": True,
                "candidates": (),
                "edges": (),
                "triage_calls": 0,
                "known_papers": 0,
                "locally_rejected": 0,
            }
        )
        ctx.store.surveys.add_preview(plan)
        ctx.store.surveys.accept(plan.id, run_id, ctx.now_iso())
        work = SurveyWork(
            run_id=run_id,
            iteration=iteration,
            plan_id=plan.id,
            scope_id=f"survey:{run_id}:{iteration}",
            topic_id=plan.topic_id,
            topic_hash=plan.topic_hash,
            started_at=ctx.now_iso(),
            paper_ids=(),
            new_paper_ids=(),
            paper_sources={},
        )
        ctx.store.surveys.save_work(work)
    saved = ctx.store.surveys.preview(work.plan_id)
    if saved is None:
        raise PermanentError("调研恢复计划缺失")
    with ctx.store.atomic():
        finish(ctx, saved, work, cancelled=True)
        cancel_children(ctx, run_id)


class SurveyStage(Stage):
    name = "survey"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        run_id = str(payload["run_id"])
        original = ctx.store.surveys.preview(str(payload["plan_id"]))
        if original is None or ctx.store.surveys.accepted_run(original.id) != run_id:
            raise PermanentError("调研计划不存在或尚未确认")
        for iteration in range(1, original.settings.rounds + 1):
            done = ctx.store.surveys.round_for(run_id, iteration)
            if done:
                if done.cancelled:
                    return
                continue
            if not _current(ctx, original):
                _cancel(ctx, original, run_id, iteration)
                return
            state = ctx.store.judgments.get_topic_state(original.topic_id)
            if state is None or state.status != "active":
                raise WaitingForWork("课题已暂停，恢复后继续调研", retry_after_seconds=30)
            plan = self._plan(ctx, original, run_id, iteration)
            work = prepare(ctx, plan, run_id, iteration)
            run_children(ctx, work)
            finish(ctx, plan, work)

    def _plan(
        self, ctx: AppContext, original: SurveyPreview, run_id: str, iteration: int
    ) -> SurveyPreview:
        if iteration == 1:
            return original
        previous = ctx.store.surveys.round_for(run_id, iteration - 1)
        if previous is None:
            raise PermanentError("上一轮调研结果缺失")
        return preview(
            ctx,
            original.topic_id,
            dois=seeds(ctx, previous, original.settings.seed_limit),
            historical=False,
            plan_id=f"survey-plan:{run_id}:{iteration}",
            internal=True,
        )
