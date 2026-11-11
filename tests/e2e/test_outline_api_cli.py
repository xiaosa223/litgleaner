"""综述骨架的 HTTP/CLI 授权边界、冻结历史与本地只读验收。"""

import fcntl
import json
import socket
import sqlite3
from contextlib import closing
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from litgleaner import cli_library
from litgleaner.application import outline
from litgleaner.application.pipeline import run_on_demand
from litgleaner.cli import app as cli_app
from litgleaner.infra.config import load_config
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness
from tests.e2e.outline_helpers import TOPIC, add_read_paper, generate, setup_outline

BASE = f"/api/topics/{TOPIC}/outline"
DOWNLOAD = f"/api/topics/{TOPIC}/exports/outline"
PORT = 48145


@pytest.fixture
def outline_ui(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    ctx, ids = setup_outline(harness)
    monkeypatch.setattr(cli_library, "context_or_exit", lambda _config: harness.ctx)
    web = create_app(
        ctx,
        LocalSecurity(PORT, launch_token="outline-test"),
        None,
        context_provider=lambda: harness.ctx,
    )
    with TestClient(web, base_url=f"http://127.0.0.1:{PORT}") as client:
        client.get("/launch", params={"token": "outline-test"})
        csrf = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
        yield harness, client, csrf, ids


def _snapshot(harness):
    path = load_config(harness.root / "config").database_path
    with closing(sqlite3.connect(path)) as connection:
        return tuple(connection.iterdump())


def _preview(client, csrf):
    response = client.post(f"{BASE}/preview", json={}, headers=csrf)
    assert response.status_code == 200, response.text
    return response.json()


def _no_external(ctx, monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("只读综述入口不应调用网络、模型或重新读取全文")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setattr(ctx.blobs, "get", forbidden)
    for source in ctx.sources.values():
        monkeypatch.setattr(source, "fetch", forbidden)
    for route in ctx.llm.routes.values():
        monkeypatch.setattr(route.llm, "complete", forbidden)
        monkeypatch.setattr(route.llm, "stream", forbidden)


def _cli(harness, *args):
    return CliRunner().invoke(
        cli_app,
        ["export", "outline", "--topic", TOPIC, *args, "--config", str(harness.root / "config")],
    )


def test_preview_is_read_only_with_fixed_inputs_and_no_external_calls(outline_ui, monkeypatch):
    harness, client, csrf, ids = outline_ui
    _no_external(harness.ctx, monkeypatch)
    before = _snapshot(harness)
    plan = _preview(client, csrf)
    assert plan["topic_id"] == TOPIC and plan["total_papers"] == len(ids)
    assert tuple(plan["input_paper_ids"]) == ids and plan["omitted_paper_ids"] == []
    assert plan["plan_hash"] and plan["producer_version"] and plan["input_hash"]
    assert 0 < plan["input_tokens"] <= plan["token_budget"]
    assert plan["cached_outline_id"] is None and plan["estimated_usd"] is None
    assert _snapshot(harness) == before and harness.llm_calls() == 0
    assert client.get(f"{BASE}/history").json() == []
    assert not harness.ctx.store.jobs.list_by_status("pending")


def test_explicit_http_confirmation_queues_then_exposes_completed_job(outline_ui):
    harness, client, csrf, ids = outline_ui
    plan = _preview(client, csrf)
    response = client.post(BASE, json={"plan_hash": plan["plan_hash"]}, headers=csrf)
    assert response.status_code == 202, response.text
    queued = response.json()
    assert queued["status"] == "pending" and queued["result"] is None
    assert harness.llm_calls() == 0
    assert run_on_demand(harness.ctx).ok
    done = client.get(f"{BASE}/jobs/{queued['job_key']}")
    assert done.status_code == 200
    assert done.json()["status"] == "done"
    assert set(done.json()["result"]["input_paper_ids"]) == set(ids)
    assert done.json()["cost_usd"] == pytest.approx(0.001)


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", BASE + "/preview"),
        ("post", BASE),
        ("get", BASE),
        ("get", BASE + "/jobs/missing"),
        ("get", BASE + "/history"),
        ("get", BASE + "/results/missing"),
        ("get", DOWNLOAD + "?result_id=missing"),
    ],
)
def test_every_outline_endpoint_requires_a_session(outline_ui, method, path):
    harness, client, csrf, _ = outline_ui
    before = _snapshot(harness)
    client.cookies.clear()
    assert getattr(client, method)(path, headers=csrf).status_code == 401
    assert _snapshot(harness) == before and harness.llm_calls() == 0


@pytest.mark.parametrize("path", [BASE, BASE + "/preview"])
def test_posts_require_csrf_and_reject_cross_origin(outline_ui, path):
    harness, client, csrf, _ = outline_ui
    before = _snapshot(harness)
    for headers in ({}, {"X-CSRF-Token": "wrong"}, {**csrf, "Origin": "https://other.test"}):
        assert client.post(path, json={}, headers=headers).status_code == 403
    assert _snapshot(harness) == before and harness.llm_calls() == 0


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"plan_hash": ""},
        {"plan_hash": 1},
        {"plan_hash": "x" * 201},
        {"plan_hash": "x", "paper_ids": []},
        {"plan_hash": "x", "execute": True},
        [],
        None,
    ],
)
def test_generation_body_rejects_missing_invalid_and_extra_fields(outline_ui, body):
    harness, client, csrf, _ = outline_ui
    before = _snapshot(harness)
    response = client.post(BASE, json=body, headers=csrf)
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"
    assert _snapshot(harness) == before and harness.llm_calls() == 0


@pytest.mark.parametrize("body", [{"execute": True}, {"paper_ids": []}, [], "unexpected"])
def test_preview_body_rejects_extra_fields_and_wrong_types(outline_ui, body):
    harness, client, csrf, _ = outline_ui
    before = _snapshot(harness)
    response = client.post(f"{BASE}/preview", json=body, headers=csrf)
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"
    assert _snapshot(harness) == before and harness.llm_calls() == 0


@pytest.mark.parametrize("limit", [0, -1, 101, "not-a-number"])
def test_history_limit_is_validated_without_writes(outline_ui, limit):
    harness, client, _, _ = outline_ui
    before = _snapshot(harness)
    assert client.get(f"{BASE}/history", params={"limit": limit}).status_code == 422
    assert _snapshot(harness) == before and harness.llm_calls() == 0


def test_history_order_limit_and_fixed_markdown_download_headers(outline_ui):
    harness, client, _, _ = outline_ui
    first = generate(harness.ctx)
    harness.clock.advance(seconds=1)
    new_id = add_read_paper(harness, 'Odd "title"\r\nX-Outline-Test: injected')
    second = generate(harness.ctx)
    assert first.result is not None and second.result is not None
    history = client.get(f"{BASE}/history").json()
    assert [item["id"] for item in history] == [second.result.id, first.result.id]
    assert client.get(f"{BASE}/history?limit=1").json() == history[:1]
    response = client.get(DOWNLOAD, params={"result_id": first.result.id})
    assert response.status_code == 200 and response.text.startswith("# 综述骨架\n")
    assert new_id not in response.text
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="litgleaner-outline.md"'
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "来源对比表" in response.text and "```bibtex" in response.text
    odd = client.get(DOWNLOAD, params={"result_id": second.result.id})
    assert new_id in odd.text
    assert odd.headers["content-disposition"] == response.headers["content-disposition"]
    assert "x-outline-test" not in odd.headers
    assert client.get(DOWNLOAD).status_code == 422
    assert client.get(DOWNLOAD, params={"result_id": ""}).status_code == 422


def test_jobs_results_downloads_and_plan_hash_cannot_cross_topic_scope(outline_ui):
    harness, client, csrf, _ = outline_ui
    plan = _preview(client, csrf)
    done = generate(harness.ctx)
    assert done.result is not None
    topic = harness.ctx.topics[TOPIC].model_copy(update={"id": "other-topic"})
    harness.ctx = replace(harness.ctx, topics={**harness.ctx.topics, topic.id: topic})
    other = "/api/topics/other-topic/outline"
    assert client.get(f"{other}/jobs/{done.job_key}").status_code == 404
    assert client.get(f"{other}/results/{done.result.id}").status_code == 404
    assert (
        client.get(
            "/api/topics/other-topic/exports/outline", params={"result_id": done.result.id}
        ).status_code
        == 404
    )
    assert client.get(f"{other}/history").json() == []
    refused = client.post(other, json={"plan_hash": plan["plan_hash"]}, headers=csrf)
    assert refused.status_code == 422 and "重新预览" in refused.json()["detail"]
    assert harness.llm_calls("outline") == 1


def test_unknown_topics_jobs_and_results_return_not_found(outline_ui):
    harness, client, csrf, _ = outline_ui
    before = _snapshot(harness)
    for path in (
        BASE + "/jobs/missing",
        BASE + "/results/missing",
        DOWNLOAD + "?result_id=missing",
        "/api/topics/missing/outline",
        "/api/topics/missing/outline/history",
    ):
        assert client.get(path).status_code == 404
    assert (
        client.post("/api/topics/missing/outline/preview", json={}, headers=csrf).status_code == 404
    )
    response = client.post("/api/topics/missing/outline", json={"plan_hash": "x"}, headers=csrf)
    assert response.status_code == 404
    assert _snapshot(harness) == before and harness.llm_calls() == 0


@pytest.mark.parametrize("missing_route", [False, True])
def test_all_get_routes_are_read_only_offline_and_keep_history(
    outline_ui, monkeypatch, missing_route
):
    harness, client, csrf, _ = outline_ui
    done = generate(harness.ctx)
    assert done.result is not None
    result_id = done.result.id
    _no_external(harness.ctx, monkeypatch)
    if missing_route:
        harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    before, calls = _snapshot(harness), harness.llm_calls()
    for _ in range(2):
        for path in (
            BASE,
            f"{BASE}/jobs/{done.job_key}",
            f"{BASE}/history",
            f"{BASE}/results/{result_id}",
            f"{DOWNLOAD}?result_id={result_id}",
        ):
            response = client.get(path)
            assert response.status_code == 200, response.text
        assert client.get(BASE).json()["result"]["id"] == result_id
    plan = _preview(client, csrf)
    if missing_route:
        assert plan["producer_version"] is None
        refused = client.post(BASE, json={"plan_hash": plan["plan_hash"]}, headers=csrf)
        assert refused.status_code == 409
        assert (
            refused.json()["task"] == "outline"
            and refused.json()["code"] == "model_configuration_required"
        )
    else:
        assert plan["cached_outline_id"] == result_id
        assert plan["estimated_usd"] == 0
    assert _snapshot(harness) == before and harness.llm_calls() == calls


def test_cli_default_preview_has_no_writes_or_external_calls(outline_ui, monkeypatch):
    harness, _, _, ids = outline_ui
    _no_external(harness.ctx, monkeypatch)
    before = _snapshot(harness)
    result = _cli(harness)
    assert result.exit_code == 0, (result.output, result.exception)
    preview = json.loads(result.stdout)
    assert tuple(preview["input_paper_ids"]) == ids and preview["plan_hash"]
    assert preview["total_papers"] == len(ids) and preview["cached_outline_id"] is None
    assert _snapshot(harness) == before and harness.llm_calls() == 0


def test_cli_explicit_execution_holds_lock_while_queuing_without_calling_model(
    outline_ui, monkeypatch
):
    harness, _, _, _ = outline_ui
    path = load_config(harness.root / "config").database_path.with_suffix(".lock")
    original_queue = outline.queue

    def queue_under_lock(*args):
        with path.open("a") as handle, pytest.raises(BlockingIOError):
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return original_queue(*args)

    monkeypatch.setattr(outline, "queue", queue_under_lock)
    preview = json.loads(_cli(harness).stdout)
    result = _cli(harness, "--execute", "--plan", preview["plan_hash"])
    assert result.exit_code == 0, (result.output, result.exception)
    status = json.loads(result.stdout)
    assert status["status"] == "pending" and status["result"] is None
    assert status["job_key"].startswith(f"outline:{TOPIC}:")
    assert len(harness.ctx.store.jobs.list_by_status("pending")) == 1
    assert harness.llm_calls() == 0


def test_cli_execution_exits_without_writes_when_another_process_holds_lock(outline_ui):
    harness, _, _, _ = outline_ui
    preview = json.loads(_cli(harness).stdout)
    path = load_config(harness.root / "config").database_path.with_suffix(".lock")
    before = _snapshot(harness)
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _cli(harness, "--execute", "--plan", preview["plan_hash"])
    assert result.exit_code == 3 and "已有流程" in result.output
    assert isinstance(result.exception, SystemExit) and "Traceback" not in result.output
    assert _snapshot(harness) == before and harness.llm_calls() == 0


@pytest.mark.parametrize("missing_route", [False, True])
def test_cli_result_exports_frozen_markdown_without_model_calls(
    outline_ui, monkeypatch, missing_route
):
    harness, client, _, ids = outline_ui
    done = generate(harness.ctx)
    assert done.result is not None
    downloaded = client.get(DOWNLOAD, params={"result_id": done.result.id}).text
    _no_external(harness.ctx, monkeypatch)
    if missing_route:
        harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    before = _snapshot(harness)
    result = _cli(harness, "--result", done.result.id)
    assert result.exit_code == 0, (result.output, result.exception)
    assert result.stdout == downloaded and result.stdout.startswith("# 综述骨架\n")
    assert all(id_ in result.stdout for id_ in ids)
    assert "来源引用" in result.stdout and "10.1000/" in result.stdout
    assert _snapshot(harness) == before and harness.llm_calls("outline") == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--execute"],
        ["--plan", "x"],
        ["--execute", "--plan", "x", "--result", "r"],
        ["--plan", "x", "--result", "r"],
        ["--unknown-option"],
        ["--result", "missing"],
        ["--execute", "--plan", "wrong-plan"],
    ],
)
def test_cli_invalid_arguments_and_unknown_result_are_friendly_and_read_only(outline_ui, args):
    harness, _, _, _ = outline_ui
    before = _snapshot(harness)
    result = _cli(harness, *args)
    assert result.exit_code == 2 and isinstance(result.exception, SystemExit)
    assert result.output and "Traceback" not in result.output
    assert _snapshot(harness) == before and harness.llm_calls() == 0


def test_cli_missing_outline_model_reports_actionable_error_without_traceback(outline_ui):
    harness, _, _, _ = outline_ui
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    plan = outline.preview(harness.ctx, TOPIC)
    before = _snapshot(harness)
    result = _cli(harness, "--execute", "--plan", plan.plan_hash)
    assert result.exit_code == 2 and isinstance(result.exception, SystemExit)
    assert (
        "outline" in result.output and "配置" in result.output and "Traceback" not in result.output
    )
    assert _snapshot(harness) == before and harness.llm_calls() == 0
