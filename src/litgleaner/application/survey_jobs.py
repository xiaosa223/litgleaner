"""限定论文集合执行正常漏斗；子任务沿用队列重试并记录同轮费用。"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.job_runner import run_stage
from litgleaner.application.stages.assess import AssessStage
from litgleaner.application.stages.base import Stage
from litgleaner.application.stages.extract import ExtractStage
from litgleaner.application.stages.fulltext import AcquireStage, ParseStage
from litgleaner.application.stages.score import ScoreStage
from litgleaner.application.stages.triage import TriageStage
from litgleaner.application.topics import triage_job
from litgleaner.domain.errors import BudgetExceeded, ModelConfigurationRequired, WaitingForWork
from litgleaner.domain.keyword_filter import filter_keywords
from litgleaner.domain.survey import SurveyWork
from litgleaner.ports.store import JobSpec


class SurveyChild(Stage):
    name = "survey_child"

    def __init__(self, delegate: Stage) -> None:
        self.delegate = delegate

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        if self.delegate.name != "triage" and (paper_id := payload.get("paper_id")):
            topic = next(iter(ctx.topics.values()))
            paper = ctx.store.papers.get(paper_id)
            if (
                paper is None
                or not filter_keywords(paper.title, paper.abstract, topic.query).passed
            ):
                return
        self.delegate.process(ctx, payload)

    def on_dead(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        self.delegate.on_dead(ctx, payload)


def _specs(ctx: AppContext, work: SurveyWork, stage: Stage) -> Iterable[JobSpec]:
    topic = ctx.topics[work.topic_id]
    ids = frozenset(work.paper_ids)
    if stage.name == "triage":
        untriaged = ctx.store.judgments.untriaged_papers(topic.id, topic.topic_hash, None)
        yield from (triage_job(id_, topic) for id_ in work.paper_ids if id_ in untriaged)
    else:
        ids = ids.intersection(ctx.store.judgments.relevant_papers(topic.id, topic.topic_hash))
        ids = frozenset(
            id_
            for id_ in ids
            if (paper := ctx.store.papers.get(id_)) is not None
            and filter_keywords(paper.title, paper.abstract, topic.query).passed
        )
        for spec in stage.plan(ctx):
            paper_id = spec.payload.get("paper_id")
            if stage.name == "score":
                assessment = ctx.store.judgments.get_assessment(spec.payload["assessment_id"])
                paper_id = assessment.paper_id if assessment else None
            if paper_id in ids:
                yield spec


def run_children(ctx: AppContext, work: SurveyWork) -> None:
    scoped = replace(
        ctx,
        topics={work.topic_id: ctx.topics[work.topic_id]},
        llm=replace(ctx.llm, scope_id=work.scope_id),
    )
    for stage in (
        TriageStage(),
        AcquireStage(),
        ParseStage(),
        ExtractStage(),
        AssessStage(),
        ScoreStage(),
    ):
        prefix = f"survey_child:{work.run_id}:{work.iteration}:{stage.name}:"
        specs = {
            prefix + spec.key: JobSpec(
                prefix + spec.key,
                "survey_child",
                {
                    **spec.payload,
                    "survey_run_id": work.run_id,
                    "survey_iteration": work.iteration,
                },
            )
            for spec in _specs(scoped, work, stage)
        }
        # 已写结果但还没标 done 的任务仍需走原队列收尾；延期任务不提前执行。
        specs.update(
            {
                job.key: JobSpec(job.key, job.stage, job.payload)
                for job in ctx.store.jobs.list_by_status("pending")
                if job.key.startswith(prefix)
            }
        )
        report = run_stage(
            scoped, SurveyChild(stage), specs=tuple(specs.values()), allowed_keys=frozenset(specs)
        )
        if report.configuration_required:
            task = stage.name if stage.name in {"triage", "extract", "assess"} else "triage"
            raise ModelConfigurationRequired(task)
        if report.budget_stopped:
            raise BudgetExceeded("调研达到今日模型预算，保留任务等待恢复")
        if any(job.key.startswith(prefix) for job in ctx.store.jobs.list_by_status("pending")):
            raise WaitingForWork("等待本轮前置任务完成", retry_after_seconds=30)


def failed(ctx: AppContext, work: SurveyWork) -> tuple[str, ...]:
    prefix = f"survey_child:{work.run_id}:{work.iteration}:"
    return tuple(
        job.key for job in ctx.store.jobs.list_by_status("dead") if job.key.startswith(prefix)
    )


def cancel_children(ctx: AppContext, run_id: str) -> None:
    """取消的父任务不再执行子任务；结束 pending，保留既有 dead 原因。"""
    prefix = f"survey_child:{run_id}:"
    for job in ctx.store.jobs.list_by_status("pending"):
        if job.key.startswith(prefix):
            ctx.store.jobs.mark_done(job.key, ctx.now_iso())
