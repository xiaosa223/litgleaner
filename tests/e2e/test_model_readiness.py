"""首次模型配置前可阅读，模型任务等待配置后原地继续。"""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from litgleaner.application import reader, translation, translation_plan
from litgleaner.application.pipeline import run_on_demand, run_stages
from litgleaner.domain.errors import BudgetExceeded, InvalidRequest, ModelConfigurationRequired
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from litgleaner.server.streaming import _sse
from tests.e2e.conftest import Harness, paper


def _remove_models(harness: Harness, *, budget: float = 0.0) -> None:
    harness.ctx = replace(
        harness.ctx, llm=replace(harness.ctx.llm, routes={}, daily_budget_usd=budget)
    )


def _processed(harness: Harness) -> str:
    harness.write_papers([paper("Read Without Models", 5)])
    assert harness.run().ok
    return harness.ctx.store.judgments.papers_with_scores()[0]


def _client(harness: Harness) -> tuple[TestClient, str]:
    security = LocalSecurity(port=48123, launch_token="readiness-test-token")
    client = TestClient(create_app(harness.ctx, security, None), base_url="http://127.0.0.1:48123")
    client.get("/launch", params={"token": "readiness-test-token"})
    return client, client.get("/api/session").json()["csrf_token"]


@pytest.mark.parametrize("stage", ["triage", "extract", "assess"])
def test_background_jobs_wait_for_models_without_failure_or_cost(harness: Harness, stage: str):
    harness.write_papers([paper("Waiting For Models", 5)])
    before = {
        "triage": ("discover",),
        "extract": ("discover", "triage", "acquire", "parse"),
        "assess": ("discover", "triage", "acquire", "parse", "extract"),
    }
    assert run_stages(harness.ctx, before[stage]).ok
    calls = harness.llm_calls()
    cost = harness.ctx.store.runs.llm_cost_since("2026-01-01")
    configured = harness.ctx
    _remove_models(harness)

    report = harness.run(stage)

    assert report.ok and report.configuration_required and not report.budget_stopped
    (result,) = report.stages
    assert result.configuration_required and not result.errors
    assert (result.done, result.retried, result.dead) == (0, 0, 0)
    assert "配置" in report.summary()
    (pending,) = [j for j in harness.ctx.store.jobs.list_by_status("pending") if j.stage == stage]
    assert pending.attempts == 0 and pending.last_error is None
    assert harness.ctx.store.jobs.list_by_status("dead") == ()
    assert harness.llm_calls() == calls
    assert harness.ctx.store.runs.llm_cost_since("2026-01-01") == cost
    harness.ctx = configured
    assert harness.run(stage).ok
    assert harness.ctx.store.jobs.get(pending.key).status == "done"
    assert harness.llm_calls() == calls + 1


def test_reading_pdf_and_target_language_cache_stays_available_without_models(harness: Harness):
    paper_id = _processed(harness)
    assert list(translation.translate_block(harness.ctx, paper_id, 2))
    calls = harness.llm_calls()
    _remove_models(harness)
    assert reader.pdf_content(harness.ctx, paper_id).startswith(b"%PDF")
    assert reader.document_view(harness.ctx, paper_id).blocks
    assert reader.paper_view(harness.ctx, paper_id).title == "Read Without Models"
    cached = translation.cached_translations(harness.ctx, paper_id)
    assert cached and all(item.cached and item.stale for item in cached.values())
    assert harness.llm_calls() == calls
    harness.ctx = replace(
        harness.ctx, settings=replace(harness.ctx.settings, reading_language="en")
    )
    assert translation.cached_translations(harness.ctx, paper_id) == {}


@pytest.mark.parametrize("operation", ["translate", "estimate", "confirm"])
def test_new_translation_requires_model_configuration(harness: Harness, operation: str):
    paper_id = _processed(harness)
    calls = harness.llm_calls()
    _remove_models(harness)
    with pytest.raises(ModelConfigurationRequired) as caught:
        if operation == "translate":
            list(translation.translate_block(harness.ctx, paper_id, 2))
        elif operation == "estimate":
            translation_plan.plan(harness.ctx, paper_id)
        else:
            translation_plan.queue(harness.ctx, paper_id)
    assert caught.value.task == "translate"
    assert harness.llm_calls() == calls
    assert not any(
        job.stage == "translate" for job in harness.ctx.store.jobs.list_by_status("pending")
    )


def test_confirmed_translation_waits_for_the_same_configuration_to_return(harness: Harness):
    paper_id = _processed(harness)
    translation_plan.queue(harness.ctx, paper_id)
    (pending,) = [
        j for j in harness.ctx.store.jobs.list_by_status("pending") if j.stage == "translate"
    ]
    configured = harness.ctx
    calls = harness.llm_calls()
    _remove_models(harness)
    report = run_on_demand(harness.ctx)
    assert report.configuration_required and report.ok
    waiting = harness.ctx.store.jobs.get(pending.key)
    assert waiting.status == "pending" and waiting.attempts == 0
    assert harness.llm_calls() == calls
    harness.ctx = configured
    assert run_on_demand(harness.ctx).ok
    assert harness.ctx.store.jobs.get(pending.key).status == "done"
    assert harness.llm_calls("translate") > 0


@pytest.mark.parametrize(
    "error,code",
    [
        (ModelConfigurationRequired("translate"), "model_configuration_required"),
        (BudgetExceeded("预算用完"), "budget_exceeded"),
        (InvalidRequest("请求无效"), "translation_error"),
        (RuntimeError("程序出错"), "internal_error"),
    ],
)
def test_stream_errors_keep_the_message_and_add_a_stable_code(error: Exception, code: str):
    def events():
        raise error
        yield  # pragma: no cover

    (event,) = list(_sse(events()))
    assert event.startswith("event: error\n")
    data = json.loads(event.split("data: ", 1)[1])
    assert data["code"] == code
    assert data["message"] == (
        "翻译时出现程序错误，详情见服务日志" if code == "internal_error" else str(error)
    )


def test_http_reading_and_translation_stream_work_without_models(harness: Harness):
    paper_id = _processed(harness)
    _remove_models(harness)
    client, csrf = _client(harness)
    assert client.get(f"/api/papers/{paper_id}").status_code == 200
    assert client.get(f"/api/papers/{paper_id}/document").status_code == 200
    assert client.get(f"/api/papers/{paper_id}/pdf").content.startswith(b"%PDF")
    assert client.get(f"/api/papers/{paper_id}/translations").json() == []
    response = client.post(
        f"/api/papers/{paper_id}/blocks/2/translate", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 200
    assert '"code": "model_configuration_required"' in response.text


@pytest.mark.parametrize("method", ["get", "post"])
def test_http_translation_estimate_and_confirmation_report_missing_models(
    harness: Harness, method: str
):
    paper_id = _processed(harness)
    calls = harness.llm_calls()
    _remove_models(harness)
    client, csrf = _client(harness)
    response = getattr(client, method)(
        f"/api/papers/{paper_id}/translation-plan", headers={"X-CSRF-Token": csrf}
    )
    assert response.status_code == 409
    assert response.json() == {
        "code": "model_configuration_required",
        "detail": str(ModelConfigurationRequired("translate")),
        "task": "translate",
    }
    assert harness.llm_calls() == calls
    assert not any(
        job.stage == "translate" for job in harness.ctx.store.jobs.list_by_status("pending")
    )
