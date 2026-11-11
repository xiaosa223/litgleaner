"""流程编排：调度、重试和失败隔离都在这里，各阶段不需要关心。"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from litgleaner.application.context import AppContext
from litgleaner.application.job_runner import StageReport, run_stage
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
from litgleaner.application.stages.survey import SurveyStage
from litgleaner.application.stages.topic_map import TopicMapStage
from litgleaner.application.stages.triage import TriageStage
from litgleaner.application.topics import sync_topics
from litgleaner.application.translation_plan import TranslatePaperStage
from litgleaner.domain.errors import ConfigError

__all__ = [
    "ON_DEMAND",
    "ON_DEMAND_NAMES",
    "STAGES",
    "STAGE_NAMES",
    "RunReport",
    "StageReport",
    "has_on_demand_work",
    "run_on_demand",
    "run_pipeline",
    "run_stage",
    "run_stages",
]

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
ON_DEMAND: tuple[Stage, ...] = (AskStage(), TranslatePaperStage(), SurveyStage(), TopicMapStage())
ON_DEMAND_NAMES: tuple[str, ...] = tuple(stage.name for stage in ON_DEMAND)


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
