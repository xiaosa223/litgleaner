"""调研 HTTP/CLI 合同；可计数来源与模型均离线，不运行调研阶段。"""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from litgleaner.application.topics import pause_topic
from litgleaner.cli import app
from litgleaner.domain.citation import ReferenceRecord
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import HistoricalCitations, HistoricalSearch, ReferenceList
from litgleaner.domain.survey import SurveySettings
from litgleaner.ports.source import FetchResult
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness

TOPIC = "alpha-topic"
BASE = f"/api/topics/{TOPIC}/survey"
RECORD = SourceRecord(title="Alpha methods", abstract="An alpha study.", doi="10.1000/methods")


class SurveySource:
    name = "survey-test"
    capabilities = frozenset({HistoricalSearch, ReferenceList, HistoricalCitations})

    def __init__(self):
        self.calls = []
        self.records = (RECORD,)

    def fetch(self, query, cursor):
        assert type(query) in self.capabilities and cursor is None
        self.calls.append(query)
        references = (
            tuple(
                ReferenceRecord(
                    query.dois[0], index, record.title, record.title, (Alias("doi", record.doi),)
                )
                for index, record in enumerate(self.records)
            )
            if isinstance(query, ReferenceList)
            else ()
        )
        return FetchResult(
            self.records, None, raw=b"offline survey response", references=references
        )


def _context(harness: Harness, source: SurveySource):
    topic = harness.ctx.topics[TOPIC]
    topic = topic.model_copy(
        update={"seeds": topic.seeds.model_copy(update={"dois": ("10.1000/seed",)})}
    )
    harness.ctx = replace(
        harness.ctx,
        sources={source.name: source},
        topics={TOPIC: topic},
        settings=replace(harness.ctx.settings, survey=SurveySettings(rounds=1)),
    )
    return harness.ctx


@pytest.fixture
def survey_ui(harness: Harness):
    source = SurveySource()
    ctx = _context(harness, source)
    web = create_app(
        ctx,
        LocalSecurity(48139, launch_token="survey-test"),
        None,
        context_provider=lambda: harness.ctx,
    )
    with TestClient(web, base_url="http://127.0.0.1:48139") as client:
        client.get("/launch", params={"token": "survey-test"})
        csrf = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
        yield harness, source, client, csrf


def _preview(client, csrf):
    response = client.post(f"{BASE}/preview", json={}, headers=csrf)
    assert response.status_code == 200, response.text
    return response.json()


def _confirm(client, csrf, plan_id):
    return client.post(BASE, json={"plan_id": plan_id}, headers=csrf)


def test_preview_persists_bounded_source_receipts_without_models_papers_or_jobs(survey_ui):
    harness, source, client, csrf = survey_ui
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    plan = _preview(client, csrf)
    assert plan["topic_id"] == TOPIC and plan["triage_calls"] == 1
    assert len(plan["candidates"]) == 1 and len(plan["sources"]) == len(source.calls) == 5
    assert {row["mode"] for row in plan["sources"]} == {
        "reviews",
        "highly_cited",
        "recent",
        "references",
        "citations",
    }
    assert all(row["raw_key"] for row in plan["sources"])
    assert harness.ctx.store.surveys.preview(plan["id"]).model_dump(mode="json") == plan
    assert harness.ctx.store.stats()["paper"] == harness.llm_calls() == 0
    assert not harness.ctx.store.jobs.list_by_status("pending")
    refused = _confirm(client, csrf, plan["id"])
    assert refused.status_code == 409
    assert refused.json()["code"] == "model_configuration_required"
    assert refused.json()["task"] == "triage" and len(source.calls) == 5


def test_plan_status_and_round_reads_are_local_even_after_configuration_changes(survey_ui):
    harness, source, client, csrf = survey_ui
    plan = _preview(client, csrf)
    queued = _confirm(client, csrf, plan["id"]).json()
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}), sources={})
    for _ in range(2):
        assert client.get(f"{BASE}/plans/{plan['id']}").json() == plan
        assert client.get(f"{BASE}/runs/{queued['run_id']}").json() == queued
        assert client.get(f"{BASE}/rounds?limit=1").json() == []
    assert len(source.calls) == 5 and harness.llm_calls() == 0
    assert harness.ctx.store.stats()["paper"] == 0


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("post", "/preview"),
        ("post", ""),
        ("get", "/plans/missing"),
        ("get", "/runs/missing"),
        ("get", "/rounds"),
    ],
)
def test_every_survey_endpoint_requires_a_session(survey_ui, method, suffix):
    _, source, client, _ = survey_ui
    client.cookies.clear()
    assert getattr(client, method)(BASE + suffix).status_code == 401
    assert source.calls == []


@pytest.mark.parametrize("suffix", ["/preview", ""])
def test_survey_posts_require_csrf_and_reject_cross_origin(survey_ui, suffix):
    _, source, client, csrf = survey_ui
    assert client.post(BASE + suffix, json={}).status_code == 403
    assert client.post(BASE + suffix, json={}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert (
        client.post(
            BASE + suffix, json={}, headers={**csrf, "Origin": "https://other.test"}
        ).status_code
        == 403
    )
    assert source.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"plan_id": ""},
        {"plan_id": 1},
        {"plan_id": "x" * 201},
        {"plan_id": "x", "execute": True},
        [],
        None,
    ],
)
def test_confirm_body_is_strict_and_cannot_inject_execution_options(survey_ui, body):
    harness, source, client, csrf = survey_ui
    response = client.post(BASE, json=body, headers=csrf)
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"
    assert not harness.ctx.store.jobs.list_by_status("pending") and not source.calls


def test_preview_rejects_unknown_body_fields_before_fetching(survey_ui):
    _, source, client, csrf = survey_ui
    response = client.post(f"{BASE}/preview", json={"execute": True}, headers=csrf)
    assert response.status_code == 422 and not source.calls


@pytest.mark.parametrize("limit", [0, -1, 1001, "not-a-number"])
def test_round_limit_is_validated_without_source_calls(survey_ui, limit):
    _, source, client, _ = survey_ui
    response = client.get(f"{BASE}/rounds", params={"limit": limit})
    assert response.status_code == 422 and not source.calls


def test_plan_and_run_ids_are_scoped_to_their_topic(survey_ui):
    harness, source, client, csrf = survey_ui
    plan = _preview(client, csrf)
    queued = _confirm(client, csrf, plan["id"]).json()
    other = harness.ctx.topics[TOPIC].model_copy(update={"id": "other-topic"})
    harness.ctx = replace(harness.ctx, topics={**harness.ctx.topics, other.id: other})
    base = "/api/topics/other-topic/survey"
    assert client.get(f"{base}/plans/{plan['id']}").status_code == 404
    assert client.get(f"{base}/runs/{queued['run_id']}").status_code == 404
    assert client.post(base, json={"plan_id": plan["id"]}, headers=csrf).status_code == 404
    assert client.get(f"{BASE}/plans/missing").status_code == 404
    assert client.get(f"{BASE}/runs/missing").status_code == 404
    assert (
        client.post("/api/topics/missing/survey/preview", json={}, headers=csrf).status_code == 404
    )
    assert client.get("/api/topics/missing/survey/rounds").status_code == 404
    assert len(source.calls) == 5


def test_repeated_confirm_returns_the_same_job_even_if_configuration_later_changes(survey_ui):
    harness, source, client, csrf = survey_ui
    plan = _preview(client, csrf)
    first = _confirm(client, csrf, plan["id"])
    assert first.status_code == 200 and first.json()["status"] == "pending"
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    assert _confirm(client, csrf, plan["id"]).json() == first.json()
    jobs = harness.ctx.store.jobs.list_by_status("pending")
    assert len(jobs) == 1 and jobs[0].key == f"survey:{first.json()['run_id']}"
    assert len(source.calls) == 5 and harness.llm_calls() == 0
    assert harness.ctx.store.stats()["paper"] == 0


def test_empty_preview_cannot_be_confirmed_or_generate_jobs(survey_ui):
    harness, source, client, csrf = survey_ui
    source.records = ()
    plan = _preview(client, csrf)
    assert not plan["candidates"] and plan["triage_calls"] == 0
    refused = _confirm(client, csrf, plan["id"])
    assert refused.status_code == 422 and "没有候选论文" in refused.json()["detail"]
    assert len(source.calls) == 5 and harness.llm_calls() == 0
    assert not harness.ctx.store.jobs.list_by_status("pending")


def test_paused_topic_allows_reviewing_preview_but_not_confirming_work(survey_ui):
    harness, source, client, csrf = survey_ui
    pause_topic(harness.ctx, TOPIC)
    plan = _preview(client, csrf)
    refused = _confirm(client, csrf, plan["id"])
    assert refused.status_code == 422 and "先启用" in refused.json()["detail"]
    assert client.get(f"{BASE}/plans/{plan['id']}").json() == plan
    assert len(source.calls) == 5 and not harness.ctx.store.jobs.list_by_status("pending")


@pytest.mark.parametrize("change", ["topic", "language", "model", "survey", "sources"])
def test_stale_unconfirmed_plan_is_rejected_without_refetch_or_enqueue(survey_ui, change):
    harness, source, client, csrf = survey_ui
    plan = _preview(client, csrf)
    ctx = harness.ctx
    if change == "topic":
        topic = ctx.topics[TOPIC].model_copy(update={"content": "different scope"})
        harness.ctx = replace(ctx, topics={TOPIC: topic})
    elif change == "language":
        harness.ctx = replace(ctx, settings=replace(ctx.settings, reading_language="en"))
    elif change == "model":
        route = replace(ctx.llm.routes["triage"], model="different-model")
        harness.ctx = replace(ctx, llm=replace(ctx.llm, routes={**ctx.llm.routes, "triage": route}))
    elif change == "survey":
        settings = ctx.settings.survey.model_copy(update={"candidate_limit": 5})
        harness.ctx = replace(ctx, settings=replace(ctx.settings, survey=settings))
    else:
        harness.ctx = replace(ctx, settings=replace(ctx.settings, survey_sources_version="changed"))
    refused = _confirm(client, csrf, plan["id"])
    assert refused.status_code == 422 and "重新预览" in refused.json()["detail"]
    assert client.get(f"{BASE}/plans/{plan['id']}").json() == plan
    assert not ctx.store.jobs.list_by_status("pending")
    assert len(source.calls) == 5 and harness.llm_calls() == 0


@pytest.fixture
def survey_cli(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    source = SurveySource()

    def context(_path):
        harness.rebuild()
        return _context(harness, source)

    monkeypatch.setattr("litgleaner.cli._context", context)
    return harness, source, CliRunner()


def _cli(fixture, *args):
    harness, _, runner = fixture
    return runner.invoke(
        app, ["topic", "survey", TOPIC, *args, "--config", str(harness.root / "config")]
    )


def _json_output(result):
    assert result.exit_code == 0, result.output or repr(result.exception)
    return json.JSONDecoder().raw_decode(result.output.lstrip())[0]


def test_cli_preview_is_persistent_and_execute_uses_latest_plan_without_refetch(survey_cli):
    harness, source, _ = survey_cli
    first = _json_output(_cli(survey_cli))
    assert first["topic_id"] == TOPIC and len(source.calls) == 5
    harness.clock.advance(seconds=1)
    latest = _json_output(_cli(survey_cli))
    assert latest["id"] != first["id"] and len(source.calls) == 10
    assert harness.ctx.store.stats()["paper"] == harness.llm_calls() == 0
    queued = _json_output(_cli(survey_cli, "--execute"))
    job = harness.ctx.store.jobs.get(f"survey:{queued['run_id']}")
    assert job.payload["plan_id"] == latest["id"] and queued["status"] == "pending"
    assert _json_output(_cli(survey_cli, "--execute")) == queued
    assert len(source.calls) == 10 and harness.llm_calls() == 0
    assert len(harness.ctx.store.jobs.list_by_status("pending")) == 1


def test_cli_explicit_plan_is_idempotent_and_does_not_refetch(survey_cli):
    harness, source, _ = survey_cli
    plan = _json_output(_cli(survey_cli))
    queued = _json_output(_cli(survey_cli, "--execute", "--plan", plan["id"]))
    assert _json_output(_cli(survey_cli, "--execute", "--plan", plan["id"])) == queued
    assert len(source.calls) == 5 and harness.ctx.store.stats()["paper"] == 0
    assert harness.llm_calls() == 0


def test_cli_requires_a_persistent_preview_before_execute(survey_cli):
    harness, source, _ = survey_cli
    result = _cli(survey_cli, "--execute")
    assert result.exit_code != 0 and "请先预览" in result.output
    assert not source.calls and not harness.ctx.store.jobs.list_by_status("pending")


def test_cli_can_preview_without_model_configuration_but_cannot_confirm(survey_cli):
    harness, source, _ = survey_cli
    harness.edit_yaml("llm.yaml", lambda d: d.update(tasks={}))
    plan = _json_output(_cli(survey_cli))
    assert plan["triage_calls"] == 1 and harness.llm_calls() == 0
    refused = _cli(survey_cli, "--execute")
    assert refused.exit_code != 0 and "尚未配置 triage" in refused.output
    assert len(source.calls) == 5 and not harness.ctx.store.jobs.list_by_status("pending")


def test_cli_plan_option_requires_execute(survey_cli):
    _, source, _ = survey_cli
    result = _cli(survey_cli, "--plan", "anything")
    assert result.exit_code == 2 and "--execute" in result.output and not source.calls


@pytest.mark.parametrize("change", ["topic", "language", "model"])
def test_cli_rejects_stale_latest_plan_and_requires_new_preview(survey_cli, change):
    harness, source, _ = survey_cli
    plan = _json_output(_cli(survey_cli))
    if change == "topic":
        harness.edit_yaml("topics/alpha-topic.yaml", lambda d: d.update(content="changed topic"))
    elif change == "language":
        harness.edit_yaml("app.yaml", lambda d: d["language"].update(reading="en"))
    else:
        harness.edit_yaml("llm.yaml", lambda d: d["tasks"]["triage"].update(model="new-model"))
    stale = _cli(survey_cli, "--execute")
    assert stale.exit_code != 0 and "重新预览" in stale.output
    assert len(source.calls) == 5 and not harness.ctx.store.jobs.list_by_status("pending")
    assert harness.ctx.store.surveys.preview(plan["id"]) is not None
    fresh = _json_output(_cli(survey_cli))
    assert fresh["id"] != plan["id"] and len(source.calls) == 10
    assert _json_output(_cli(survey_cli, "--execute"))["status"] == "pending"
    assert len(source.calls) == 10 and harness.llm_calls() == 0
