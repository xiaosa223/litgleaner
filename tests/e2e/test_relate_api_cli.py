"""HTTP会话/CSRF、只读历史、首次打开和全库回填的明确确认边界。"""

import json
import sqlite3
from contextlib import closing
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from typer.testing import CliRunner

from litgleaner import cli_library
from litgleaner.application import reader_open, relate_backfill, relation_feedback
from litgleaner.application.pipeline import run_on_demand
from litgleaner.cli import app
from litgleaner.cli_shared import try_run_lock
from litgleaner.domain.errors import InvalidRequest
from litgleaner.infra.config import load_config
from litgleaner.infra.db.schema import reading_state
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.memory_helpers import add
from tests.e2e.relate_flow_helpers import generate, respond
from tests.e2e.relate_helpers import HERE, pair, source

PORT = 48156


def snapshot(harness):
    path = load_config(harness.root / "config").database_path
    with closing(sqlite3.connect(path)) as db:
        return tuple(db.iterdump())


@pytest.fixture
def ui(harness, monkeypatch):
    ids = pair(harness)
    monkeypatch.setattr(cli_library, "context_or_exit", lambda _: harness.ctx)
    web = create_app(
        harness.ctx,
        LocalSecurity(PORT, launch_token="relate-test"),
        None,
        context_provider=lambda: harness.ctx,
    )
    with TestClient(web, base_url=f"http://127.0.0.1:{PORT}") as client:
        client.get("/launch", params={"token": "relate-test"})
        csrf = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
        yield harness, client, csrf, ids


def test_get_history_and_preview_are_read_only_and_no_model(ui, monkeypatch):
    harness, client, csrf, (a, _) = ui
    before = snapshot(harness)

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only endpoint attempted a write or model")

    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    monkeypatch.setattr(type(harness.llm), "complete", forbidden)
    assert client.get(f"/api/papers/{a}/relations").json()["relations"] == []
    assert client.get(f"/api/papers/{a}/relations/history").json() == []
    plan = client.post(f"/api/papers/{a}/relations/preview", headers=csrf).json()
    assert plan["pairs_count"] == 1 and plan["plan_hash"] and plan["estimated_usd"] > 0
    backfill = client.post("/api/memory/backfill/preview", headers=csrf).json()
    assert backfill["pairs_count"] == 1 and backfill["pending_pairs"] == 1
    assert snapshot(harness) == before and harness.llm_calls() == 0


def test_api_confirmation_only_queues_then_returns_frozen_result_and_feedback(ui, monkeypatch):
    harness, client, csrf, (a, b) = ui
    respond(harness, monkeypatch)
    plan = client.post(f"/api/papers/{a}/relations/preview", headers=csrf).json()
    response = client.post(
        f"/api/papers/{a}/relations", json={"plan_hash": plan["plan_hash"]}, headers=csrf
    )
    assert response.status_code == 202 and harness.llm_calls() == 0
    key = response.json()["job_key"]
    assert run_on_demand(harness.ctx).ok
    result = client.get(f"/api/papers/{a}/relations/jobs/{key}").json()
    assert result["status"] == "done" and result["cost_usd"] == 0.01
    batch = result["result"]
    assert client.get(f"/api/papers/{a}/relations/results/{batch['id']}").json() == batch
    assert client.get(f"/api/papers/{b}/relations/results/{batch['id']}").status_code == 404
    view = client.get(f"/api/papers/{a}/relations").json()
    assert {row["status"] for row in view["checks"]} == {"current"}
    row = view["relations"][0]
    body = {"action": "change", "kind": "same_problem", "request_id": "api-change"}
    changed = client.post(f"/api/relations/{row['id']}/feedback", json=body, headers=csrf)
    assert changed.status_code == 200 and changed.json()["source"] == "user"
    assert (
        client.post(f"/api/relations/{row['id']}/feedback", json=body, headers=csrf).json()
        == changed.json()
    )


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/memory/backfill", {"plan_hash": "bad"}),
        ("/api/memory/backfill/preview", None),
        ("/api/papers/target/relations", {"plan_hash": "bad"}),
        ("/api/papers/target/relations/preview", None),
        ("/api/papers/target/open", None),
        ("/api/relations/target/feedback", {"action": "reject", "request_id": "bad"}),
    ],
)
def test_mutating_routes_require_csrf_and_trusted_origin(ui, path, body):
    harness, client, csrf, _ = ui
    before = snapshot(harness)
    assert client.post(path, json=body).status_code == 403
    assert (
        client.post(
            path, json=body, headers={**csrf, "Origin": "https://attacker.invalid"}
        ).status_code
        == 403
    )
    assert snapshot(harness) == before


def test_first_open_explicit_write_is_idempotent_and_preserves_read(ui):
    harness, client, csrf, _ = ui
    paper_id = add(harness, "first-open", library=False)
    assert client.get(f"/api/papers/{paper_id}").status_code == 200
    assert harness.ctx.store.admissions.get(paper_id) is None
    first = client.post(f"/api/papers/{paper_id}/open", headers=csrf)
    assert first.status_code == 200 and first.json()["status"] == "reading"
    event = harness.ctx.store.admissions.get(paper_id)
    assert client.post(f"/api/papers/{paper_id}/open", headers=csrf).json() == first.json()
    harness.ctx.store.reader.set_state(paper_id, "read", {"page": 3}, harness.ctx.now_iso())
    assert reader_open.open_paper(harness.ctx, paper_id).status == "read"
    assert harness.ctx.store.admissions.get(paper_id) == event
    assert harness.llm_calls() == 0


def test_whole_library_backfill_deduplicates_pairs_uses_cache_and_skips_user_pairs(ui, monkeypatch):
    harness, _client, _csrf, (a, b) = ui
    respond(harness, monkeypatch)
    first = relate_backfill.preview(harness.ctx)
    assert set(first.library_paper_ids) == {a, b} and first.pairs_count == 1
    results = relate_backfill.queue(harness.ctx, first.plan_hash)
    assert len(results) == 2 and harness.llm_calls() == 0
    assert run_on_demand(harness.ctx).ok and harness.llm_calls("relate") == 1
    second = relate_backfill.preview(harness.ctx)
    assert second.cached_batches == 2 and second.pending_pairs == 0 and second.estimated_usd == 0
    relate_backfill.queue(harness.ctx, second.plan_hash)
    assert run_on_demand(harness.ctx).ok and harness.llm_calls("relate") == 1
    row = harness.ctx.store.relations.for_paper(a)[0]
    relation_feedback.apply(harness.ctx, row.id, "confirm", "pin-backfill")
    assert relate_backfill.preview(harness.ctx).pairs_count == 0


def test_backfill_rechecks_entire_library_before_confirmation_and_rolls_back(ui):
    harness, _, _, _ = ui
    plan = relate_backfill.preview(harness.ctx)
    add(harness, "new-library-member")
    before = snapshot(harness)
    with pytest.raises(InvalidRequest, match="重新预览"):
        relate_backfill.queue(harness.ctx, plan.plan_hash)
    assert snapshot(harness) == before


def test_cli_default_preview_zero_writes_and_execute_requires_plan_and_shared_lock(ui):
    harness, _, _, _ = ui
    runner = CliRunner()
    command = ["memory", "backfill", "--config", str(harness.root / "config")]
    before = snapshot(harness)
    preview = runner.invoke(app, command)
    assert preview.exit_code == 0, preview.output
    plan = json.loads(preview.stdout)
    assert plan["pairs_count"] == 1 and snapshot(harness) == before
    for args in (["--execute"], ["--plan-hash", plan["plan_hash"]]):
        assert runner.invoke(app, [*command, *args]).exit_code != 0
    config = load_config(harness.root / "config")
    with try_run_lock(config) as acquired:
        assert acquired
        blocked = runner.invoke(app, [*command, "--execute", "--plan-hash", plan["plan_hash"]])
        assert blocked.exit_code != 0 and snapshot(harness) == before
    queued = runner.invoke(app, [*command, "--execute", "--plan-hash", plan["plan_hash"]])
    assert queued.exit_code == 0, queued.output
    assert len(json.loads(queued.stdout)) == 2 and harness.llm_calls() == 0


def test_no_relate_route_keeps_get_and_preview_available_but_cannot_execute(ui):
    harness, client, csrf, (a, _) = ui
    harness.ctx = replace(
        harness.ctx,
        llm=replace(
            harness.ctx.llm,
            routes={key: value for key, value in harness.ctx.llm.routes.items() if key != "relate"},
        ),
    )
    assert client.get(f"/api/papers/{a}/relations").status_code == 200
    plan = client.post(f"/api/papers/{a}/relations/preview", headers=csrf).json()
    assert plan["producer_version"] is None and plan["estimated_usd"] is None
    response = client.post(
        f"/api/papers/{a}/relations", json={"plan_hash": plan["plan_hash"]}, headers=csrf
    )
    assert response.status_code == 409 and response.json()["task"] == "relate"


def test_change_after_library_exit_returns_422_with_no_feedback_write(ui, monkeypatch):
    harness, client, csrf, (a, b) = ui
    respond(harness, monkeypatch)
    generate(harness, a)
    row = harness.ctx.store.relations.for_paper(a)[0]
    harness.ctx.store._db.write(delete(reading_state).where(reading_state.c.paper_id == b))
    before = snapshot(harness)
    response = client.post(
        f"/api/relations/{row.id}/feedback",
        headers=csrf,
        json={"action": "change", "kind": "same_problem", "request_id": "outside"},
    )
    assert response.status_code == 422 and "仍在文库" in response.json()["detail"]
    assert snapshot(harness) == before


def test_backfill_each_doc_is_read_once_outside_write_transaction(ui, monkeypatch):
    harness, _client, _csrf, _ids = ui
    for index in range(8):
        id_ = add(harness, f"backfill-{index}")
        source(harness, id_, HERE)
    original, calls = type(harness.ctx.blobs).get, []

    def read(self, key):
        assert harness.ctx.store._db._active is None
        calls.append(key)
        return original(self, key)

    monkeypatch.setattr(type(harness.ctx.blobs), "get", read)
    plan = relate_backfill.preview(harness.ctx)
    assert len(calls) == len(set(calls)) == 10
    calls.clear()
    relate_backfill.queue(harness.ctx, plan.plan_hash)
    assert len(calls) == len(set(calls)) == 10
