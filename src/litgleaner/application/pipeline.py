"""流程编排：调度、重试和失败隔离都在这里，各阶段不需要关心。"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from litgleaner.application.context import AppContext
from litgleaner.application.qa import AskStage
from litgleaner.application.stages.archive import ArchiveStage
from litgleaner.application.stages.assess import AssessStage
from litgleaner.application.stages.base import Stage
from litgleaner.application.stages.discover import DiscoverStage
from litgleaner.application.stages.extract import ExtractStage
from litgleaner.application.stages.fulltext import AcquireStage, ParseStage
from litgleaner.application.stages.library_save import LibrarySaveStage
from litgleaner.application.stages.publish import PublishStage
from litgleaner.application.stages.score import ScoreStage
from litgleaner.application.stages.select import SelectStage
from litgleaner.application.stages.triage import TriageStage
from litgleaner.application.topics import sync_topics
from litgleaner.application.translation_plan import TranslatePaperStage
from litgleaner.domain.errors import (
    BudgetExceeded,
    ConfigError,
    Deferred,
    ModelConfigurationRequired,
    PermanentError,
    RateLimited,
)
from litgleaner.ports.store import Job

logger = logging.getLogger(__name__)
_ERROR_LIMIT = 500

STAGES: tuple[Stage, ...] = (
    DiscoverStage(),
    TriageStage(),
    AcquireStage(),
    ParseStage(),
    ExtractStage(),
    AssessStage(),
    ScoreStage(),
    ArchiveStage(),
    SelectStage(),
    PublishStage(),
    LibrarySaveStage(),
)
STAGE_NAMES: tuple[str, ...] = tuple(stage.name for stage in STAGES)
# 按需阶段（第 15.1 节）：用户在领读器里确认后才有任务，没有 plan；排在后台阶段之前执行
ON_DEMAND: tuple[Stage, ...] = (AskStage(), TranslatePaperStage())
ON_DEMAND_NAMES: tuple[str, ...] = tuple(stage.name for stage in ON_DEMAND)


@dataclass(frozen=True)
class StageReport:
    name: str
    done: int = 0
    retried: int = 0
    dead: int = 0
    errors: tuple[str, ...] = ()
    budget_stopped: bool = False
    configuration_required: bool = False


@dataclass(frozen=True)
class RunReport:
    stages: tuple[StageReport, ...]
    warnings: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not any(stage.retried or stage.dead or stage.errors for stage in self.stages)

    @property
    def budget_stopped(self) -> bool:
        return any(stage.budget_stopped for stage in self.stages)

    @property
    def configuration_required(self) -> bool:
        return any(stage.configuration_required for stage in self.stages)

    def summary(self) -> str:
        lines = [
            f"{s.name}: 完成 {s.done}，待重试 {s.retried}，死信 {s.dead}"
            + ("，预算已用完" if s.budget_stopped else "")
            + ("，等待完成模型配置" if s.configuration_required else "")
            for s in self.stages
            if s.done
            or s.retried
            or s.dead
            or s.errors
            or s.budget_stopped
            or s.configuration_required
        ]
        lines += [error for stage in self.stages for error in stage.errors]
        lines += [f"警告：{warning}" for warning in self.warnings]
        return "\n".join(lines) or "没有需要处理的内容"


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
        if _deferred_too_long(ctx, job, now):
            hours = ctx.settings.max_defer_hours
            return True, _bury(
                ctx, stage, job, f"推迟超过 {hours} 小时仍未完成：{message}", now.isoformat()
            )
        later = (now + timedelta(seconds=error.retry_after_seconds)).isoformat()
        ctx.store.jobs.postpone(job.key, message, later, now.isoformat())
        return False, message
    if isinstance(error, PermanentError) or attempts >= ctx.settings.max_attempts:
        return True, _bury(ctx, stage, job, message, now.isoformat())
    delay = ctx.settings.backoff_seconds * 2 ** (attempts - 1)
    next_run = (now + timedelta(seconds=delay)).isoformat()
    ctx.store.jobs.mark_retry(job.key, message, next_run, now.isoformat())
    return False, message


def run_stage(ctx: AppContext, stage: Stage) -> StageReport:
    now = ctx.now_iso()
    errors: list[str] = []
    try:
        for spec in stage.plan(ctx):
            ctx.store.jobs.enqueue(spec, now, reopen=stage.reopen_when_planned)
    except Exception as error:
        logger.exception("阶段 %s 规划失败", stage.name)
        errors.append(f"{stage.name} 规划失败：{type(error).__name__}: {error}"[:_ERROR_LIMIT])
    done = retried = dead = 0
    for job in ctx.store.jobs.due(stage.name, now):
        try:
            stage.process(ctx, job.payload)
        except ModelConfigurationRequired as error:
            logger.info("%s", error)
            return StageReport(
                stage.name, done, retried, dead, tuple(errors), configuration_required=True
            )
        except BudgetExceeded as error:
            logger.warning("%s", error)
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


def run_stages(ctx: AppContext, names: Sequence[str]) -> RunReport:
    warnings = sync_topics(ctx)
    selected = [stage for stage in (*ON_DEMAND, *STAGES) if stage.name in names]
    reports = tuple(run_stage(ctx, stage) for stage in selected)
    dead = len(ctx.store.jobs.list_by_status("dead"))
    if dead:
        warnings = (*warnings, f"有 {dead} 个死信任务未处理，用 litgleaner jobs list 查看")
    return RunReport(reports, warnings)


def run_pipeline(ctx: AppContext, only: str | None = None) -> RunReport:
    names = (*ON_DEMAND_NAMES, *STAGE_NAMES)
    if only is not None and only not in names:
        raise ConfigError(f"未知阶段：{only}，可选：{', '.join(names)}")
    return run_stages(ctx, (only,) if only else names)


def has_on_demand_work(ctx: AppContext) -> bool:
    now = ctx.now_iso()
    return any(ctx.store.jobs.due(name, now) for name in ON_DEMAND_NAMES)


def run_on_demand(ctx: AppContext) -> RunReport:
    """只处理用户确认过的按需任务。常驻服务在两次定时运行之间频繁调用，不同步主题。"""
    return RunReport(tuple(run_stage(ctx, stage) for stage in ON_DEMAND))
