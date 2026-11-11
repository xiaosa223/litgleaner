"""复用真实队列和假模型，验证子任务范围与原有执行合同。"""

from dataclasses import replace

import pytest

from litgleaner.application.job_runner import StageReport, run_stage
from litgleaner.application.pipeline import StageReport as PublicStageReport
from litgleaner.application.pipeline import run_stage as public_run_stage
from litgleaner.application.stages.base import Stage
from litgleaner.domain.errors import (
    PermanentError,
    RateLimited,
    StillProcessing,
    TransientError,
    WaitingForWork,
)
from litgleaner.ports.store import JobSpec
from tests.e2e.conftest import Harness


def _spec(name: str, stage: str = "survey_child") -> JobSpec:
    return JobSpec(name, stage, {"name": name})


class ScopedStage(Stage):
    name = "survey_child"

    def __init__(self, planned=None, *, model=False):
        self.planned = planned
        self.model = model
        self.plans = 0
        self.seen = []
        self.errors = {}
        self.buried = []

    def plan(self, ctx):
        self.plans += 1
        if self.planned is None:
            raise AssertionError("scoped 执行不应调用全局 plan")
        return self.planned

    def process(self, ctx, payload):
        name = payload["name"]
        self.seen.append(name)
        if error := self.errors.pop(name, None):
            raise error
        if self.model:
            ctx.llm.call("triage", {}, name, {"type": "object"})

    def on_dead(self, ctx, payload):
        self.buried.append(payload["name"])


def test_pipeline_keeps_public_import_compatibility():
    assert public_run_stage is run_stage and PublicStageReport is StageReport


def test_explicit_specs_bypass_plan_and_leave_unscoped_and_other_stage_jobs_untouched(
    harness: Harness,
):
    unrelated, other = _spec("daily"), _spec("other", "triage")
    for spec in (unrelated, other):
        harness.ctx.store.jobs.enqueue(spec, harness.ctx.now_iso())
    before = tuple(harness.ctx.store.jobs.get(spec.key) for spec in (unrelated, other))
    stage = ScopedStage(model=True)
    report = run_stage(
        harness.ctx, stage, specs=(_spec("selected"),), allowed_keys=frozenset({"selected"})
    )
    assert report == StageReport("survey_child", done=1)
    assert stage.plans == 0 and stage.seen == ["selected"]
    assert harness.llm_calls("triage") == 1
    assert tuple(harness.ctx.store.jobs.get(spec.key) for spec in (unrelated, other)) == before


def test_none_specs_uses_original_plan_and_runs_all_due_jobs(harness: Harness):
    harness.ctx.store.jobs.enqueue(_spec("existing"), harness.ctx.now_iso())
    stage = ScopedStage((_spec("planned"),))
    report = run_stage(harness.ctx, stage)
    assert report.done == 2 and stage.plans == 1
    assert stage.seen == ["existing", "planned"]


def test_empty_specs_skip_plan_and_can_resume_only_existing_selected_job(harness: Harness):
    for name in ("selected", "unscoped"):
        harness.ctx.store.jobs.enqueue(_spec(name), harness.ctx.now_iso())
    stage = ScopedStage()
    report = run_stage(harness.ctx, stage, specs=(), allowed_keys=frozenset({"selected"}))
    assert report.done == 1 and stage.plans == 0 and stage.seen == ["selected"]
    assert harness.ctx.store.jobs.get("unscoped").status == "pending"


def test_empty_allowed_keys_enqueues_but_executes_nothing(harness: Harness):
    stage = ScopedStage()
    report = run_stage(harness.ctx, stage, specs=(_spec("selected"),), allowed_keys=frozenset())
    assert report == StageReport("survey_child")
    assert stage.plans == 0 and not stage.seen
    assert harness.ctx.store.jobs.get("selected").status == "pending"


@pytest.mark.parametrize("reopen", [False, True])
def test_completed_job_is_skipped_unless_stage_explicitly_reopens_it(harness: Harness, reopen):
    stage = ScopedStage()
    spec = _spec("selected")
    assert run_stage(harness.ctx, stage, specs=(spec,)).done == 1
    stage.reopen_when_planned = reopen
    report = run_stage(harness.ctx, stage, specs=(spec,), allowed_keys=frozenset({spec.key}))
    assert report.done == int(reopen) and len(stage.seen) == 1 + int(reopen)
    assert harness.ctx.store.jobs.get(spec.key).status == "done"


@pytest.mark.parametrize("stop", ["budget", "configuration"])
def test_scoped_budget_or_missing_model_keeps_jobs_pending_and_resumes_without_attempts(
    harness: Harness, stop
):
    stage = ScopedStage(model=True)
    ctx = harness.ctx
    gateway = (
        replace(ctx.llm, daily_budget_usd=0) if stop == "budget" else replace(ctx.llm, routes={})
    )
    stopped_ctx = replace(ctx, llm=gateway)
    specs = (_spec("a-selected"), _spec("z-unscoped"))
    report = run_stage(stopped_ctx, stage, specs=specs, allowed_keys=frozenset({"a-selected"}))
    assert report.budget_stopped == (stop == "budget")
    assert report.configuration_required == (stop == "configuration")
    assert report.done == report.retried == report.dead == 0 and not report.errors
    assert harness.llm_calls() == 0
    assert all(ctx.store.jobs.get(spec.key).attempts == 0 for spec in specs)
    assert all(ctx.store.jobs.get(spec.key).status == "pending" for spec in specs)
    resumed = run_stage(ctx, stage, specs=(), allowed_keys=frozenset({"a-selected"}))
    assert resumed.done == 1 and harness.llm_calls("triage") == 1
    assert ctx.store.jobs.get("z-unscoped").status == "pending"


def test_scoped_transient_failure_keeps_backoff_and_does_not_run_before_due(harness: Harness):
    stage = ScopedStage()
    stage.errors["selected"] = TransientError("temporary")
    scope = frozenset({"selected"})
    report = run_stage(harness.ctx, stage, specs=(_spec("selected"),), allowed_keys=scope)
    assert report.retried == 1 and report.dead == 0 and "temporary" in report.errors[0]
    assert harness.ctx.store.jobs.get("selected").attempts == 1
    assert run_stage(harness.ctx, stage, specs=(), allowed_keys=scope).done == 0
    harness.clock.advance(seconds=harness.ctx.settings.backoff_seconds)
    assert run_stage(harness.ctx, stage, specs=(), allowed_keys=scope).done == 1
    assert stage.seen == ["selected", "selected"]


def test_scoped_permanent_failure_runs_cleanup_and_preserves_other_jobs(harness: Harness):
    stage = ScopedStage()
    stage.errors["bad"] = PermanentError("invalid")
    specs = (_spec("bad"), _spec("good"), _spec("unscoped"))
    report = run_stage(harness.ctx, stage, specs=specs, allowed_keys=frozenset({"bad", "good"}))
    assert (report.done, report.retried, report.dead) == (1, 0, 1)
    assert stage.buried == ["bad"] and harness.ctx.store.jobs.get("bad").attempts == 1
    assert harness.ctx.store.jobs.get("unscoped").status == "pending"


def test_scoped_deferred_error_preserves_attempts_and_expiry_cleanup(harness: Harness):
    stage = ScopedStage()
    stage.errors["selected"] = StillProcessing("waiting", retry_after_seconds=60)
    scope = frozenset({"selected"})
    first = run_stage(harness.ctx, stage, specs=(_spec("selected"),), allowed_keys=scope)
    job = harness.ctx.store.jobs.get("selected")
    assert first.retried == 1 and job.attempts == 0 and job.first_deferred_at
    assert run_stage(harness.ctx, stage, specs=(), allowed_keys=scope).done == 0
    harness.clock.advance(hours=harness.ctx.settings.max_defer_hours + 1)
    stage.errors["selected"] = StillProcessing("waiting", retry_after_seconds=60)
    final = run_stage(harness.ctx, stage, specs=(), allowed_keys=scope)
    assert final.dead == 1 and "推迟超过" in final.errors[0] and stage.buried == ["selected"]


def test_rate_limit_stops_scoped_batch_without_touching_remaining_jobs(harness: Harness):
    stage = ScopedStage()
    stage.errors["a"] = RateLimited("quota", retry_after_seconds=60)
    report = run_stage(
        harness.ctx, stage, specs=(_spec("a"), _spec("b")), allowed_keys=frozenset({"a", "b"})
    )
    assert report.retried == 1 and report.done == 0 and len(report.errors) == 2
    assert stage.seen == ["a"]
    assert harness.ctx.store.jobs.get("a").attempts == 0
    assert harness.ctx.store.jobs.get("b").status == "pending"


def test_local_work_wait_has_no_external_deferral_expiry(harness: Harness):
    stage = ScopedStage()
    scope = frozenset({"selected"})
    stage.errors["selected"] = WaitingForWork("paused", retry_after_seconds=30)
    first = run_stage(harness.ctx, stage, specs=(_spec("selected"),), allowed_keys=scope)
    original = harness.ctx.store.jobs.get("selected")
    assert first.retried == 1 and original.attempts == 0
    harness.clock.advance(hours=harness.ctx.settings.max_defer_hours + 1)
    stage.errors["selected"] = WaitingForWork("children pending", retry_after_seconds=30)
    waiting = run_stage(harness.ctx, stage, specs=(), allowed_keys=scope)
    job = harness.ctx.store.jobs.get("selected")
    assert waiting.retried == 1 and waiting.dead == 0 and not stage.buried
    assert job.status == "pending" and job.attempts == 0
    assert job.first_deferred_at == original.first_deferred_at
    harness.clock.advance(seconds=30)
    assert run_stage(harness.ctx, stage, specs=(), allowed_keys=scope).done == 1


def test_global_planning_error_is_reported_but_existing_due_jobs_still_execute(harness: Harness):
    stage = ScopedStage()
    harness.ctx.store.jobs.enqueue(_spec("existing"), harness.ctx.now_iso())
    report = run_stage(harness.ctx, stage)
    assert report.done == 1 and stage.plans == 1
    assert len(report.errors) == 1 and "规划失败" in report.errors[0]
