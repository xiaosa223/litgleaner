"""整篇翻译（第 12.3、15.1 节）：先估算费用，用户确认后排入任务队列，由按需阶段执行。"""

import pytest
from fastapi.testclient import TestClient

from litgleaner.application import translation_plan
from litgleaner.application.pipeline import run_on_demand
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, paper

PORT = 48124
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def paper_id(harness: Harness) -> str:
    harness.write_papers([paper("Top Paper", 5)])
    harness.run()
    (found,) = harness.ctx.store.judgments.papers_with_scores()
    return found


@pytest.fixture
def client(harness: Harness, paper_id: str) -> TestClient:
    security = LocalSecurity(port=PORT, launch_token="launch")
    app = create_app(harness.ctx, security, static_dir=None)
    test_client = TestClient(app, base_url=BASE)
    test_client.get("/launch", params={"token": "launch"}, follow_redirects=False)
    csrf = test_client.get("/api/session").json()["csrf_token"]
    test_client.headers["X-CSRF-Token"] = csrf
    return test_client


def test_plan_estimates_without_calling_the_model(harness: Harness, paper_id: str):
    plan = translation_plan.plan(harness.ctx, paper_id)
    assert plan.paragraphs >= 2
    assert plan.pending == plan.paragraphs
    assert plan.characters > 0
    assert plan.estimated_usd is not None and plan.estimated_usd > 0
    assert plan.state == "idle"
    assert harness.ctx.store.runs.llm_call_count("translate") == 0  # 估算不产生费用


def test_queue_then_run_translates_every_paragraph(harness: Harness, paper_id: str):
    queued = translation_plan.queue(harness.ctx, paper_id)
    assert queued.state == "queued"
    assert translation_plan.queue(harness.ctx, paper_id).state == "queued"  # 重复确认不重复排队

    report = run_on_demand(harness.ctx)
    assert report.ok

    after = translation_plan.plan(harness.ctx, paper_id)
    assert after.pending == 0 and after.state == "done"
    calls = harness.ctx.store.runs.llm_call_count("translate")
    assert calls >= 1

    run_on_demand(harness.ctx)  # 已完成的任务不会再跑
    assert harness.ctx.store.runs.llm_call_count("translate") == calls


def test_estimate_falls_back_to_none_without_price(
    harness: Harness, paper_id: str, monkeypatch: pytest.MonkeyPatch
):
    llm = harness.ctx.llm.routes["translate"].llm
    monkeypatch.setattr(llm, "price", lambda _model: None)
    assert translation_plan.plan(harness.ctx, paper_id).estimated_usd is None


def test_http_plan_and_confirm(client: TestClient, paper_id: str, harness: Harness):
    url = f"/api/papers/{paper_id}/translation-plan"
    plan = client.get(url).json()
    assert plan["state"] == "idle" and plan["pending"] == plan["paragraphs"]

    response = client.post(url)
    assert response.status_code == 202
    assert response.json()["state"] == "queued"
    without_csrf = TestClient(client.app, base_url=BASE, cookies=client.cookies)
    assert without_csrf.post(url).status_code == 403

    run_on_demand(harness.ctx)
    assert client.get(url).json()["state"] == "done"


def test_plan_for_unknown_paper_is_404(client: TestClient):
    assert client.get("/api/papers/nope/translation-plan").status_code == 404


def test_confirmed_job_does_not_use_a_changed_model(harness: Harness, paper_id: str):
    translation_plan.queue(harness.ctx, paper_id)
    harness.edit_yaml("llm.yaml", lambda data: data["tasks"]["translate"].update(model="new-model"))
    harness.rebuild()
    run_on_demand(harness.ctx)
    assert harness.llm_calls("translate") == 0
    old = harness.ctx.store.jobs.list_by_status("dead")
    assert len(old) == 1 and "重新确认" in old[0].last_error
    assert translation_plan.plan(harness.ctx, paper_id).state == "idle"
    translation_plan.queue(harness.ctx, paper_id)
    run_on_demand(harness.ctx)
    assert translation_plan.plan(harness.ctx, paper_id).pending == 0


def test_unverified_translation_is_retried_instead_of_reported_done(
    harness: Harness, paper_id: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(translation_plan, "translate_blocks", lambda *_args: iter(()))
    translation_plan.queue(harness.ctx, paper_id)
    run_on_demand(harness.ctx)
    after = translation_plan.plan(harness.ctx, paper_id)
    assert after.pending > 0 and after.state == "retrying"
    assert "译文校验" in after.error


def test_explicit_confirmation_retries_a_dead_translation(harness: Harness, paper_id: str):
    translation_plan.queue(harness.ctx, paper_id)
    (job,) = harness.ctx.store.jobs.list_by_status("pending")
    harness.ctx.store.jobs.mark_dead(job.key, "temporary failure", harness.ctx.now_iso())
    retried = translation_plan.queue(harness.ctx, paper_id)
    assert retried.state == "queued" and retried.error is None
    run_on_demand(harness.ctx)
    assert translation_plan.plan(harness.ctx, paper_id).pending == 0
