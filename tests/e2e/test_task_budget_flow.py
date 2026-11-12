"""任务预算沿真实队列、HTTP 和 SSE 生效；费用与原有全局预算独立。"""

from dataclasses import replace

import pytest

from litgleaner.application.job_runner import run_stage
from litgleaner.application.stages.base import Stage
from litgleaner.application.stages.extract import extraction_producer_version
from litgleaner.domain.errors import RateLimited, TaskBudgetExceeded
from litgleaner.ports.store import JobSpec
from tests.e2e.conftest import Harness, paper
from tests.e2e.test_model_settings import SETTINGS, connected, draft
from tests.e2e.test_model_settings import model_ui as mock_model_ui

model_ui = mock_model_ui


class BudgetStage(Stage):
    name = "task_budget_test"

    def plan(self, ctx):
        return ()

    def process(self, ctx, payload):
        ctx.llm.call(payload["task"], {}, "paper", {"type": "object"})


def _limited(ctx, task, amount):
    route = replace(ctx.llm.routes[task], daily_budget_usd=amount)
    return replace(ctx, llm=replace(ctx.llm, routes={**ctx.llm.routes, task: route}))


def test_task_budget_postpones_only_its_jobs_and_other_task_can_finish(harness: Harness):
    ctx = _limited(harness.ctx, "triage", 0)
    for key, task in (("a-blocked", "triage"), ("b-ready", "extract")):
        ctx.store.jobs.enqueue(JobSpec(key, BudgetStage.name, {"task": task}), ctx.now_iso())

    result = run_stage(ctx, BudgetStage())

    assert (result.done, result.retried, result.dead) == (1, 1, 0)
    assert not result.budget_stopped
    blocked = ctx.store.jobs.get("a-blocked")
    assert blocked.status == "pending" and blocked.attempts == 0
    assert blocked.first_deferred_at is None
    assert not ctx.store.jobs.due(BudgetStage.name, "2026-11-03T23:59:59.999999+00:00")
    assert [
        job.key for job in ctx.store.jobs.due(BudgetStage.name, "2026-11-04T00:00:00+00:00")
    ] == ["a-blocked"]
    assert "TaskBudgetExceeded" in blocked.last_error
    assert ctx.store.jobs.get("b-ready").status == "done"
    assert harness.llm_calls("triage") == 0 and harness.llm_calls("extract") == 1


def test_budget_wait_never_expires_as_external_failure_and_can_resume(harness: Harness):
    ctx = _limited(harness.ctx, "triage", 0)
    spec = JobSpec("waiting", BudgetStage.name, {"task": "triage"})
    ctx.store.jobs.enqueue(spec, ctx.now_iso())
    assert run_stage(ctx, BudgetStage()).retried == 1
    harness.clock.advance(hours=ctx.settings.max_defer_hours + 24)
    assert run_stage(ctx, BudgetStage()).dead == 0
    waiting = ctx.store.jobs.get(spec.key)
    assert waiting.attempts == 0 and waiting.status == "pending"
    harness.clock.advance(days=1)
    result = run_stage(_limited(ctx, "triage", 1), BudgetStage())
    assert result.done == 1 and result.dead == 0
    assert ctx.store.jobs.get(spec.key).attempts == 0 and harness.llm_calls() == 1


def test_global_budget_keeps_original_stop_rule_even_with_task_cap(harness: Harness):
    ctx = _limited(harness.ctx, "triage", 0)
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=0))
    ctx.store.jobs.enqueue(JobSpec("waiting", BudgetStage.name, {"task": "triage"}), ctx.now_iso())
    result = run_stage(ctx, BudgetStage())
    job = ctx.store.jobs.get("waiting")
    assert result.budget_stopped and result.retried == result.dead == 0
    assert job.attempts == 0 and job.first_deferred_at is None
    assert harness.llm_calls() == 0


def test_long_budget_wait_does_not_expire_first_external_wait_after_recovery(harness, monkeypatch):
    ctx = _limited(harness.ctx, "triage", 0)
    spec = JobSpec("waiting", BudgetStage.name, {"task": "triage"})
    ctx.store.jobs.enqueue(spec, ctx.now_iso())
    assert run_stage(ctx, BudgetStage()).retried == 1
    harness.clock.advance(hours=ctx.settings.max_defer_hours + 24)

    def limited(_request):
        raise RateLimited("provider busy", retry_after_seconds=60)

    monkeypatch.setattr(harness.llm, "complete", limited)
    restored = _limited(ctx, "triage", 1)
    first = run_stage(restored, BudgetStage())
    job = ctx.store.jobs.get(spec.key)
    assert first.retried == 1 and first.dead == 0
    assert job.attempts == 0 and job.status == "pending"
    assert job.first_deferred_at == restored.now_iso()
    harness.clock.advance(hours=ctx.settings.max_defer_hours + 1)
    expired = run_stage(restored, BudgetStage())
    assert expired.dead == 1 and ctx.store.jobs.get(spec.key).status == "dead"
    assert "推迟超过" in ctx.store.jobs.get(spec.key).last_error


def test_local_budget_wait_clears_existing_external_deadline(harness, monkeypatch):
    def limited(_request):
        raise RateLimited("provider busy", retry_after_seconds=60)

    monkeypatch.setattr(harness.llm, "complete", limited)
    ctx = harness.ctx
    spec = JobSpec("waiting", BudgetStage.name, {"task": "triage"})
    ctx.store.jobs.enqueue(spec, ctx.now_iso())
    assert run_stage(ctx, BudgetStage()).retried == 1
    assert ctx.store.jobs.get(spec.key).first_deferred_at == ctx.now_iso()
    harness.clock.advance(hours=1)
    assert run_stage(_limited(ctx, "triage", 0), BudgetStage()).retried == 1
    assert ctx.store.jobs.get(spec.key).first_deferred_at is None


def test_budget_postpone_and_deadline_clear_roll_back_together(harness, monkeypatch):
    ctx = _limited(harness.ctx, "triage", 0)
    spec = JobSpec("waiting", BudgetStage.name, {"task": "triage"})
    ctx.store.jobs.enqueue(spec, ctx.now_iso())
    original = ctx.store.jobs.get(spec.key)

    def failed_clear(_key):
        raise RuntimeError("deadline clear failed")

    monkeypatch.setattr(ctx.store.jobs, "clear_external_wait", failed_clear)
    with pytest.raises(RuntimeError, match="deadline clear failed"):
        run_stage(ctx, BudgetStage())
    assert ctx.store.jobs.get(spec.key) == original
    assert [job.key for job in ctx.store.jobs.due(BudgetStage.name, ctx.now_iso())] == [spec.key]


@pytest.mark.parametrize("stop", ["global_budget", "missing_route"])
def test_stage_pause_resets_selected_external_waits_and_preserves_other_jobs(
    harness, monkeypatch, stop
):
    ctx = harness.ctx
    for key, stage in (
        ("a", BudgetStage.name),
        ("b", BudgetStage.name),
        ("z-unselected", BudgetStage.name),
        ("other", "other_stage"),
    ):
        ctx.store.jobs.enqueue(JobSpec(key, stage, {"task": "triage"}), ctx.now_iso())
        ctx.store.jobs.postpone(key, "previous external wait", ctx.now_iso(), ctx.now_iso())
    untouched = tuple(ctx.store.jobs.get(key) for key in ("z-unselected", "other"))
    harness.clock.advance(hours=ctx.settings.max_defer_hours + 24)
    paused = replace(
        ctx,
        llm=replace(ctx.llm, daily_budget_usd=0)
        if stop == "global_budget"
        else replace(ctx.llm, routes={}),
    )
    selected = frozenset({"a", "b"})
    report = run_stage(paused, BudgetStage(), allowed_keys=selected)
    assert report.budget_stopped == (stop == "global_budget")
    assert report.configuration_required == (stop == "missing_route")
    assert report.dead == report.retried == 0
    assert all(ctx.store.jobs.get(key).first_deferred_at is None for key in selected)
    assert tuple(ctx.store.jobs.get(key) for key in ("z-unselected", "other")) == untouched

    def limited(_request):
        raise RateLimited("first external retry after pause", retry_after_seconds=60)

    monkeypatch.setattr(harness.llm, "complete", limited)
    recovered = run_stage(ctx, BudgetStage(), allowed_keys=selected)
    assert recovered.retried == 1 and recovered.dead == 0
    assert ctx.store.jobs.get("a").first_deferred_at == ctx.now_iso()
    assert all(ctx.store.jobs.get(key).attempts == 0 for key in selected)


def test_sse_reports_task_budget_without_calling_model(harness: Harness):
    harness.write_papers([paper("Task Budget SSE", 5)])
    assert harness.run().ok
    paper_id = harness.ctx.store.judgments.papers_with_scores()[0]
    harness.ctx = _limited(harness.ctx, "translate", 0)
    before = harness.llm_calls()
    ui = connected(harness)
    response = ui.client.post(f"/api/papers/{paper_id}/blocks/2/translate")
    assert response.status_code == 200 and "event: error" in response.text
    assert '"code": "budget_exceeded"' in response.text
    assert '"task": "translate"' in response.text and "event: end" not in response.text
    assert harness.llm_calls() == before


def test_http_task_budget_has_stable_code_and_task(harness: Harness, monkeypatch):
    ui = connected(harness)

    def task_budget_error(*_args):
        raise TaskBudgetExceeded("extract", 1, 1, 60)

    monkeypatch.setattr("litgleaner.server.app.reader.paper_view", task_budget_error)
    response = ui.client.get("/api/papers/budgeted")
    assert response.status_code == 402
    assert response.json()["code"] == "budget_exceeded"
    assert response.json()["task"] == "extract"
    assert harness.llm_calls() == 0


def test_qa_pending_view_reports_ask_cap_without_queue_execution(harness: Harness):
    harness.write_papers([paper("Task Budget Question", 5)])
    assert harness.run().ok
    harness.edit_yaml(
        "llm.yaml",
        lambda data: data["tasks"].update(
            {"ask": {"provider": "main", "model": "cheap", "prompt": "ask", "daily_budget_usd": 0}}
        ),
    )
    harness.rebuild()
    paper_id = harness.ctx.store.judgments.papers_with_scores()[0]
    before = harness.llm_calls()
    ui = connected(harness)
    response = ui.client.post(
        f"/api/papers/{paper_id}/qa", json={"text": "研究问题是什么？", "request_id": "budget-test"}
    )
    assert response.status_code == 202
    assert response.json()["state"] == response.json()["code"] == "budget_exceeded"
    question_id = response.json()["question"]["id"]
    status = ui.client.get(f"/api/papers/{paper_id}/qa/{question_id}").json()
    assert status["state"] == "budget_exceeded" and status["answer"] is None
    assert harness.llm_calls() == before


def test_settings_roundtrip_caps_and_probe_remains_global_only(model_ui):
    ui = model_ui
    body = draft(task_budgets_usd={"triage": 0, "translate": 0.5})
    before = ui.harness.llm_calls()
    saved = ui.client.put(SETTINGS, json=body)
    assert saved.status_code == 200 and ui.harness.llm_calls() == before
    caps = {task["task"]: task["daily_budget_usd"] for task in saved.json()["tasks"]}
    assert caps["triage"] == 0 and caps["translate"] == 0.5 and caps["extract"] is None
    current = ui.runtime.snapshot()
    assert current.llm.routes["triage"].daily_budget_usd == 0
    preserved = ui.client.put(SETTINGS, json=draft()).json()
    assert (
        next(task for task in preserved["tasks"] if task["task"] == "triage")["daily_budget_usd"]
        == 0
    )
    probe = ui.client.post(SETTINGS + "/test", json=body)
    assert probe.status_code == 200 and probe.json()["ok"]
    assert ui.harness.llm_calls("configuration_test") == 2
    assert ui.harness.llm_calls("triage") == ui.harness.llm_calls("translate") == 0
    cleared = ui.client.put(SETTINGS, json=draft(task_budgets_usd={"triage": None})).json()
    assert (
        next(task for task in cleared["tasks"] if task["task"] == "triage")["daily_budget_usd"]
        is None
    )
    assert ui.runtime.snapshot().llm.routes["translate"].daily_budget_usd == 0.5


@pytest.mark.parametrize("cap", [True, "0.5", -1, [], {"secret": "never-echo"}])
def test_invalid_task_cap_never_writes_or_calls_provider(model_ui, cap):
    ui = model_ui
    previous = (ui.harness.root / "config" / "llm.yaml").read_bytes()
    response = ui.client.put(SETTINGS, json=draft(task_budgets_usd={"triage": cap}))
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"
    assert "never-echo" not in response.text
    assert not ui.peer.requests and not ui.secrets.writes
    assert (ui.harness.root / "config" / "llm.yaml").read_bytes() == previous


def test_cap_change_preserves_producer_and_cached_translation(harness: Harness):
    harness.write_papers([paper("Budget Cached Translation", 5)])
    assert harness.run().ok
    paper_id = harness.ctx.store.judgments.papers_with_scores()[0]
    ui = connected(harness)
    first = ui.client.post(f"/api/papers/{paper_id}/blocks/2/translate")
    assert '"cached": false' in first.text
    limited = _limited(harness.ctx, "translate", 0)
    ui = connected(replace(harness, ctx=limited))
    before = harness.llm_calls()
    cached = ui.client.post(f"/api/papers/{paper_id}/blocks/2/translate")
    assert '"cached": true' in cached.text and "event: error" not in cached.text
    assert harness.llm_calls() == before
    assert extraction_producer_version(harness.ctx) == extraction_producer_version(
        _limited(harness.ctx, "extract", 0)
    )
