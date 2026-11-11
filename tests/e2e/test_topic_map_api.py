"""课题地图 HTTP 合同：显式授权、配置指纹与本地只读投影。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from litgleaner.application.pipeline import run_on_demand
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness
from tests.e2e.topic_map_helpers import TOPIC, add_paper, setup_map

BASE = f"/api/topics/{TOPIC}/map"


@pytest.fixture
def map_ui(harness: Harness):
    ctx, ids = setup_map(harness, count=3)
    web = create_app(
        ctx,
        LocalSecurity(48143, launch_token="map-test"),
        None,
        context_provider=lambda: harness.ctx,
    )
    with TestClient(web, base_url="http://127.0.0.1:48143") as client:
        client.get("/launch", params={"token": "map-test"})
        csrf = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
        yield harness, client, csrf, ids


def _preview(client, csrf):
    response = client.post(f"{BASE}/preview", json={}, headers=csrf)
    assert response.status_code == 200, response.text
    return response.json()


def _queue(client, csrf, plan_hash):
    return client.post(BASE, json={"plan_hash": plan_hash}, headers=csrf)


def _generate(harness, client, csrf):
    preview = _preview(client, csrf)
    response = _queue(client, csrf, preview["plan_hash"])
    assert response.status_code == 200, response.text
    queued = response.json()
    assert queued["status"] == "pending" and queued["result"] is None
    report = run_on_demand(harness.ctx)
    assert report.ok, report.summary()
    completed = client.get(f"{BASE}/jobs/{queued['job_key']}")
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "done"
    return preview, completed.json()


def test_preview_only_reads_current_facts_and_never_calls_or_enqueues_models(map_ui):
    harness, client, csrf, ids = map_ui
    before = harness.ctx.store.stats()
    plan = _preview(client, csrf)
    assert plan["topic_id"] == TOPIC and plan["total_papers"] == 3
    assert set(plan["input_paper_ids"]) == set(ids) and not plan["omitted_paper_ids"]
    assert 0 < plan["input_tokens"] <= plan["token_budget"]
    assert plan["producer_version"] and plan["cached_map_id"] is None
    assert plan["estimated_usd"] is None and plan["plan_hash"]
    assert harness.ctx.store.stats() == before and harness.llm_calls() == 0
    assert not harness.ctx.store.jobs.list_by_status("pending")
    assert client.get(f"{BASE}/history").json() == []
    initial = client.get(BASE).json()
    assert initial["result"] is None and set(initial["ungrouped"]) == set(ids)


def test_confirm_is_idempotent_and_generates_cleaned_groups_with_one_billed_call(map_ui):
    harness, client, csrf, ids = map_ui
    plan = _preview(client, csrf)
    first = _queue(client, csrf, plan["plan_hash"])
    assert first.status_code == 200
    assert _queue(client, csrf, plan["plan_hash"]).json() == first.json()
    assert harness.llm_calls("map") == 0
    assert len(harness.ctx.store.jobs.list_by_status("pending")) == 1
    assert run_on_demand(harness.ctx).ok
    done = client.get(f"{BASE}/jobs/{first.json()['job_key']}").json()
    assert done["status"] == "done" and done["cost_usd"] == pytest.approx(0.001)
    saved = done["result"]
    # FakeLLM 返回五组；只有三篇论文，两个空组经领域规则清理。
    assert len(saved["groups"]) == 3 and all(group["paper_ids"] for group in saved["groups"])
    assert {id_ for group in saved["groups"] for id_ in group["paper_ids"]} == set(ids)
    assert saved["warnings"] and not saved["ungrouped"]
    assert _queue(client, csrf, plan["plan_hash"]).json() == done
    assert run_on_demand(harness.ctx).ok and harness.llm_calls("map") == 1
    cached = _preview(client, csrf)
    assert cached["cached_map_id"] == saved["id"] and cached["estimated_usd"] == 0
    assert harness.ctx.store.runs.llm_cost_for_scope(saved["scope_id"]) == pytest.approx(0.001)


def test_all_get_views_remain_local_with_existing_results_and_no_model_route(map_ui):
    harness, client, csrf, ids = map_ui
    _, done = _generate(harness, client, csrf)
    saved = done["result"]
    harness.ctx = replace(
        harness.ctx,
        llm=replace(
            harness.ctx.llm, routes={k: v for k, v in harness.ctx.llm.routes.items() if k != "map"}
        ),
    )
    before = harness.llm_calls()
    for _ in range(2):
        current = client.get(BASE)
        assert current.status_code == 200 and current.json()["result"] == saved
        assert current.json()["total_papers"] == len(ids)
        assert client.get(f"{BASE}/jobs/{done['job_key']}").json() == done
        assert client.get(f"{BASE}/history?limit=1").json() == [saved]
        assert client.get(f"{BASE}/results/{saved['id']}").json() == saved
    plan = _preview(client, csrf)
    assert plan["producer_version"] is None and plan["total_papers"] == 3
    refused = _queue(client, csrf, plan["plan_hash"])
    assert refused.status_code == 409 and refused.json()["task"] == "map"
    assert refused.json()["code"] == "model_configuration_required"
    assert harness.llm_calls() == before
    assert not harness.ctx.store.jobs.list_by_status("pending")


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("post", "/preview"),
        ("post", ""),
        ("get", ""),
        ("get", "/jobs/missing"),
        ("get", "/history"),
        ("get", "/results/missing"),
    ],
)
def test_every_map_endpoint_requires_a_session(map_ui, method, suffix):
    harness, client, _, _ = map_ui
    client.cookies.clear()
    assert getattr(client, method)(BASE + suffix).status_code == 401
    assert harness.llm_calls() == 0 and not harness.ctx.store.jobs.list_by_status("pending")


@pytest.mark.parametrize("suffix", ["", "/preview"])
def test_map_posts_require_csrf_and_block_cross_origin(map_ui, suffix):
    harness, client, csrf, _ = map_ui
    assert client.post(BASE + suffix, json={}).status_code == 403
    assert client.post(BASE + suffix, json={}, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    response = client.post(BASE + suffix, json={}, headers={**csrf, "Origin": "https://other.test"})
    assert response.status_code == 403
    assert harness.llm_calls() == 0 and not harness.ctx.store.jobs.list_by_status("pending")


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"plan_hash": ""},
        {"plan_hash": 1},
        {"plan_hash": "x" * 201},
        {"plan_hash": "x", "paper_ids": []},
        [],
        None,
    ],
)
def test_generation_body_rejects_missing_invalid_or_extra_fields(map_ui, body):
    harness, client, csrf, _ = map_ui
    response = client.post(BASE, json=body, headers=csrf)
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"
    assert harness.llm_calls() == 0 and not harness.ctx.store.jobs.list_by_status("pending")


@pytest.mark.parametrize("body", [{"execute": True}, [], "unexpected"])
def test_preview_body_is_strict(map_ui, body):
    harness, client, csrf, _ = map_ui
    response = client.post(f"{BASE}/preview", json=body, headers=csrf)
    assert response.status_code == 422 and response.json()["code"] == "invalid_request"
    assert harness.llm_calls() == 0


@pytest.mark.parametrize("limit", [0, -1, 101, "not-a-number"])
def test_history_limit_validation_is_local(map_ui, limit):
    harness, client, _, _ = map_ui
    response = client.get(f"{BASE}/history", params={"limit": limit})
    assert response.status_code == 422 and harness.llm_calls() == 0


def test_job_result_and_plan_hash_cannot_cross_topic_scope(map_ui):
    harness, client, csrf, _ = map_ui
    plan, done = _generate(harness, client, csrf)
    topic = harness.ctx.topics[TOPIC].model_copy(update={"id": "other-topic"})
    harness.ctx = replace(harness.ctx, topics={**harness.ctx.topics, topic.id: topic})
    other = "/api/topics/other-topic/map"
    assert client.get(f"{other}/jobs/{done['job_key']}").status_code == 404
    assert client.get(f"{other}/results/{done['result']['id']}").status_code == 404
    refused = client.post(other, json={"plan_hash": plan["plan_hash"]}, headers=csrf)
    assert refused.status_code == 422 and "重新预览" in refused.json()["detail"]
    assert client.get(f"{other}/history").json() == []
    assert client.get(f"{BASE}/jobs/missing").status_code == 404
    assert client.get(f"{BASE}/results/missing").status_code == 404
    assert harness.llm_calls("map") == 1


def test_unknown_topic_reads_and_preview_return_404(map_ui):
    harness, client, csrf, _ = map_ui
    base = "/api/topics/missing/map"
    assert client.get(base).status_code == 404
    assert client.get(f"{base}/history").status_code == 404
    assert client.post(f"{base}/preview", json={}, headers=csrf).status_code == 404
    assert harness.llm_calls() == 0


@pytest.mark.parametrize("change", ["topic", "reading_language", "model", "budget", "new_paper"])
def test_changed_configuration_or_candidates_invalidate_confirmation(map_ui, change):
    harness, client, csrf, _ = map_ui
    plan = _preview(client, csrf)
    ctx = harness.ctx
    if change == "topic":
        topic = ctx.topics[TOPIC].model_copy(update={"background": "changed research"})
        harness.ctx = replace(ctx, topics={TOPIC: topic})
    elif change == "reading_language":
        harness.ctx = replace(ctx, settings=replace(ctx.settings, reading_language="en"))
    elif change == "model":
        route = replace(ctx.llm.routes["map"], model="different-map")
        harness.ctx = replace(ctx, llm=replace(ctx.llm, routes={**ctx.llm.routes, "map": route}))
    elif change == "budget":
        settings = ctx.settings.map.model_copy(update={"token_budget": 16000})
        harness.ctx = replace(ctx, settings=replace(ctx.settings, map=settings))
    else:
        add_paper(harness, "A new alpha paper")
    refused = _queue(client, csrf, plan["plan_hash"])
    assert refused.status_code == 422 and "重新预览" in refused.json()["detail"]
    assert not ctx.store.jobs.list_by_status("pending") and harness.llm_calls() == 0


def test_new_paper_only_changes_read_view_until_explicit_confirmation(map_ui):
    harness, client, csrf, _ = map_ui
    _, done = _generate(harness, client, csrf)
    paper_id = add_paper(harness, "A later alpha paper")
    current = client.get(BASE).json()
    assert current["result"] == done["result"]
    assert current["new_paper_ids"] == [paper_id] and paper_id in current["ungrouped"]
    assert current["total_papers"] == 4
    assert client.get(f"{BASE}/history").json() == [done["result"]]
    assert run_on_demand(harness.ctx).ok and harness.llm_calls("map") == 1
    assert not harness.ctx.store.jobs.list_by_status("pending")


def test_empty_topic_can_be_previewed_but_cannot_generate(map_ui):
    harness, client, csrf, _ = map_ui
    topic = harness.ctx.topics[TOPIC].model_copy(update={"content": "untriaged scope"})
    harness.ctx = replace(harness.ctx, topics={TOPIC: topic})
    plan = _preview(client, csrf)
    assert plan["total_papers"] == 0 and plan["input_paper_ids"] == []
    assert plan["estimated_usd"] == 0
    refused = _queue(client, csrf, plan["plan_hash"])
    assert refused.status_code == 422 and "没有可发送" in refused.json()["detail"]
    assert harness.llm_calls() == 0 and not harness.ctx.store.jobs.list_by_status("pending")
