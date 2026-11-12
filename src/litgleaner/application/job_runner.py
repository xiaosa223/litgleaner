"""共享任务执行器：规划入队、重试、预算和失败隔离，也支持显式任务范围。"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.stages.base import Stage
from litgleaner.domain.errors import (
    BudgetExceeded,
    Deferred,
    ModelConfigurationRequired,
    PermanentError,
    RateLimited,
    WaitingForWork,
)
from litgleaner.ports.store import Job, JobSpec

logger = logging.getLogger(__name__)
_ERROR_LIMIT = 500
_AUTOMATIC_PAPER_STAGES = frozenset({"triage", "acquire", "parse", "extract", "assess", "score"})


@dataclass(frozen=True)
class StageReport:
    name: str
    done: int = 0
    retried: int = 0
    dead: int = 0
    errors: tuple[str, ...] = ()
    budget_stopped: bool = False
    configuration_required: bool = False


def _deferred_too_long(ctx: AppContext, job: Job, now: datetime) -> bool:
    """推迟不计失败次数，但外部任务可能永远卡住，所以总时长要有上限。"""
    if job.first_deferred_at is None:
        return False
    started = datetime.fromisoformat(job.first_deferred_at)
    return now - started > timedelta(hours=ctx.settings.max_defer_hours)


def _bury(ctx: AppContext, stage: Stage, job: Job, message: str, now: str) -> str:
    """标记死信和善后要么都成功，要么都不生效；善后出错也不能中断其他任务。"""
    try:
        with ctx.store.atomic():
            ctx.store.jobs.mark_dead(job.key, message, now)
            stage.on_dead(ctx, job.payload)
    except Exception as error:
        logger.exception("任务 %s 的善后失败", job.key)
        message = f"{message}；善后失败：{type(error).__name__}: {error}"[:_ERROR_LIMIT]
        ctx.store.jobs.mark_dead(job.key, message, now)
    return message


def _fail(ctx: AppContext, stage: Stage, job: Job, error: Exception) -> tuple[bool, str]:
    """记录失败并返回（是否进入死信，错误说明）。"""
    message = f"{type(error).__name__}: {error}"[:_ERROR_LIMIT]
    attempts = job.attempts + 1
    now = ctx.clock()
    if isinstance(error, Deferred):
        if not isinstance(error, WaitingForWork) and _deferred_too_long(ctx, job, now):
            hours = ctx.settings.max_defer_hours
            return True, _bury(
                ctx, stage, job, f"推迟超过 {hours} 小时仍未完成：{message}", now.isoformat()
            )
        later = (now + timedelta(seconds=error.retry_after_seconds)).isoformat()
        with ctx.store.atomic():
            ctx.store.jobs.postpone(job.key, message, later, now.isoformat())
            if isinstance(error, WaitingForWork):
                ctx.store.jobs.clear_external_wait(job.key)
        return False, message
    if isinstance(error, PermanentError) or attempts >= ctx.settings.max_attempts:
        return True, _bury(ctx, stage, job, message, now.isoformat())
    delay = ctx.settings.backoff_seconds * 2 ** (attempts - 1)
    next_run = (now + timedelta(seconds=delay)).isoformat()
    ctx.store.jobs.mark_retry(job.key, message, next_run, now.isoformat())
    return False, message


def _enqueue(
    ctx: AppContext,
    stage: Stage,
    now: str,
    specs: tuple[JobSpec, ...] | None,
    held: frozenset[str],
) -> list[str]:
    try:
        for spec in stage.plan(ctx) if specs is None else specs:
            if _available(ctx, spec.payload, held):
                ctx.store.jobs.enqueue(spec, now, reopen=stage.reopen_when_planned)
    except Exception as error:
        logger.exception("阶段 %s 规划失败", stage.name)
        return [f"{stage.name} 规划失败：{type(error).__name__}: {error}"[:_ERROR_LIMIT]]
    return []


def _available(ctx: AppContext, payload: Mapping[str, Any], held: frozenset[str]) -> bool:
    if not held:
        return True
    paper_id = payload.get("paper_id")
    if paper_id is None and "assessment_id" in payload:
        assessment = ctx.store.judgments.get_assessment(payload["assessment_id"])
        paper_id = assessment.paper_id if assessment else None
    return paper_id not in held


def _clear_external_waits(
    ctx: AppContext,
    jobs: tuple[Job, ...],
    allowed_keys: frozenset[str] | None,
    held: frozenset[str],
) -> None:
    keys = tuple(
        job.key
        for job in jobs
        if job.first_deferred_at is not None
        and (allowed_keys is None or job.key in allowed_keys)
        and _available(ctx, job.payload, held)
    )
    if keys:
        with ctx.store.atomic():
            for key in keys:
                ctx.store.jobs.clear_external_wait(key)


def run_stage(
    ctx: AppContext,
    stage: Stage,
    *,
    specs: tuple[JobSpec, ...] | None = None,
    allowed_keys: frozenset[str] | None = None,
) -> StageReport:
    """默认执行整个阶段；显式 specs 跳过全局规划，allowed_keys 限制执行范围。"""
    now = ctx.now_iso()
    held = (
        ctx.store.surveys.held_paper_ids() if stage.name in _AUTOMATIC_PAPER_STAGES else frozenset()
    )
    errors = _enqueue(ctx, stage, now, specs, held)
    done = retried = dead = 0
    due = ctx.store.jobs.due(stage.name, now)
    for index, job in enumerate(due):
        if allowed_keys is not None and job.key not in allowed_keys:
            continue
        if not _available(ctx, job.payload, held):
            continue
        try:
            stage.process(ctx, job.payload)
        except ModelConfigurationRequired as error:
            logger.info("%s", error)
            _clear_external_waits(ctx, due[index:], allowed_keys, held)
            return StageReport(
                stage.name, done, retried, dead, tuple(errors), configuration_required=True
            )
        except BudgetExceeded as error:
            logger.warning("%s", error)
            _clear_external_waits(ctx, due[index:], allowed_keys, held)
            return StageReport(stage.name, done, retried, dead, tuple(errors), budget_stopped=True)
        except Exception as error:
            logger.warning("任务失败 %s", job.key, exc_info=not isinstance(error, PermanentError))
            is_dead, message = _fail(ctx, stage, job, error)
            dead += is_dead
            retried += not is_dead
            errors.append(f"{stage.name} [{job.key}]：{message}")
            if isinstance(error, RateLimited):
                errors.append(f"{stage.name} 触发限频或额度用完，本轮剩余任务稍后再处理")
                break
        else:
            ctx.store.jobs.mark_done(job.key, ctx.now_iso())
            done += 1
    return StageReport(stage.name, done, retried, dead, tuple(errors))
