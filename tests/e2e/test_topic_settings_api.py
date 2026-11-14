"""课题设置（第 7.1、7.3、12.8 节）：在领读器里启用、暂停、修改研究画像和回填历史论文。"""

import pytest
import yaml
from fastapi.testclient import TestClient

from litgleaner.application.pipeline import run_pipeline
from litgleaner.application.topics import pause_topic
from litgleaner.infra.config import load_config
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from litgleaner.service import ModelRuntime
from tests.e2e.conftest import Harness, paper
from tests.e2e.test_model_settings import MemorySecrets

TOPIC = "alpha-topic"
BASE = f"/api/topics/{TOPIC}"
PROFILE = {
    "name": "Alpha 2",
    "background": "新的背景",
    "content": "alpha research",
    "goals": [{"id": "g1", "text": "alpha goal"}, {"id": None, "text": "新目标"}],
}


class UI:
    def __init__(self, harness: Harness):
        self.harness = harness
        self.runtime = ModelRuntime(
            load_config(harness.root / "config"), harness.ctx, MemorySecrets()
        )
        app = create_app(
            harness.ctx,
            LocalSecurity(48153, launch_token="settings-test"),
            None,
            context_provider=self.runtime.snapshot,
            model_settings=self.runtime,
            topic_settings=self.runtime,
        )
        self.client = TestClient(app, base_url="http://127.0.0.1:48153")
        self.client.get("/launch", params={"token": "settings-test"})
        self.client.headers["X-CSRF-Token"] = self.client.get("/api/session").json()["csrf_token"]

    def state(self, topic_id: str = TOPIC):
        return self.runtime.snapshot().store.judgments.get_topic_state(topic_id)

    def file(self) -> dict:
        path = self.harness.root / "config" / "topics" / f"{TOPIC}.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture
def ui(harness: Harness) -> UI:
    return UI(harness)


def test_detail_returns_the_editable_profile(ui):
    detail = ui.client.get(BASE).json()
    assert detail["background"] == "alpha background" and detail["content"] == "alpha research"
    assert (detail["status"], detail["terms"]) == ("active", 1)
    assert ui.client.get("/api/topics/nope").status_code == 404


def test_pause_then_activate_without_model_calls(ui):
    paused = ui.client.post(f"{BASE}/pause", json={})
    assert paused.status_code == 200 and paused.json()["status"] == "paused"
    assert ui.state().status == "paused"
    active = ui.client.post(f"{BASE}/activate", json={})
    assert active.status_code == 200 and active.json()["status"] == "active"
    assert ui.state().status == "active" and ui.harness.llm_calls() == 0


def test_a_topic_without_search_terms_cannot_be_activated(ui):
    created = ui.client.post(
        "/api/topics", json={"name": "Beta", "background": "b", "content": "c", "goals": ["g"]}
    ).json()["id"]
    response = ui.client.post(f"/api/topics/{created}/activate", json={})
    assert response.status_code == 422 and response.json()["detail"] == "topic_query_missing"
    assert ui.state(created) is None


def test_profile_edit_keeps_query_and_goal_ids_and_refreshes_an_active_topic(ui):
    old_hash = ui.state().topic_hash
    response = ui.client.put(f"{BASE}/profile", json=PROFILE)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["changed"] is True and saved["topic"]["status"] == "active"
    assert [g["id"] for g in saved["topic"]["goals"]] == ["g1", "g2"]  # 界面据此换成带编号的目标
    data = ui.file()
    assert data["name"] == "Alpha 2" and data["background"] == "新的背景"
    assert data["goals"] == [{"id": "g1", "text": "alpha goal"}, {"id": "g2", "text": "新目标"}]
    assert data["query"]["include"] == ["alpha"]
    assert (ui.harness.root / "config" / "topics" / f"{TOPIC}.yaml.bak").is_file()
    assert not (ui.harness.root / "config" / "topics" / f"{TOPIC}.glossary.yaml").exists()
    topic = ui.runtime.snapshot().topics[TOPIC]
    assert ui.state().topic_hash == topic.topic_hash != old_hash
    assert ui.client.get("/api/topics").json()[0]["name"] == "Alpha 2"


def test_renaming_only_does_not_change_judgments(ui):
    before = ui.state()
    body = {**PROFILE, "background": "alpha background", "goals": [PROFILE["goals"][0]]}
    response = ui.client.put(f"{BASE}/profile", json=body)
    assert response.status_code == 200 and response.json()["changed"] is False
    assert ui.state() == before


def test_profile_edit_keeps_a_paused_topic_paused(ui):
    pause_topic(ui.runtime.snapshot(), TOPIC)
    response = ui.client.put(f"{BASE}/profile", json=PROFILE)
    assert response.status_code == 200 and response.json()["topic"]["status"] == "paused"
    assert ui.state().status == "paused"


@pytest.mark.parametrize(
    "change",
    [
        {"background": " "},
        {"goals": []},
        {"goals": [{"id": "g7", "text": "编号不存在"}]},
        {"extra": 1},
    ],
)
def test_invalid_profile_is_rejected_without_writing(ui, change):
    response = ui.client.put(f"{BASE}/profile", json={**PROFILE, **change})
    assert response.status_code == 422
    assert ui.file()["name"] == "Alpha"


def test_profile_edit_refuses_a_hand_edited_file(ui):
    ui.harness.edit_yaml(f"topics/{TOPIC}.yaml", lambda d: d.update(content="手改"))
    response = ui.client.put(f"{BASE}/profile", json=PROFILE)
    assert response.status_code == 422 and response.json()["detail"] == "topic_changed_on_disk"


def test_saving_the_query_of_an_active_topic_refreshes_its_state(ui):
    body = {"include": [{"term": "beta", "synonyms": []}], "exclude": [], "glossary": []}
    assert ui.client.put(f"{BASE}/query", json=body).status_code == 200
    assert ui.state().topic_hash == ui.runtime.snapshot().topics[TOPIC].topic_hash


def test_backfill_previews_then_retriages_history_under_the_new_profile(ui):
    ui.harness.write_papers([paper("alpha one"), paper("alpha two")])
    assert run_pipeline(ui.runtime.snapshot()).ok
    triaged = ui.harness.llm_calls("triage")
    assert triaged == 2
    assert ui.client.post(f"{BASE}/backfill/preview", json={}).json()["papers"] == 0
    ui.client.put(f"{BASE}/profile", json=PROFILE)
    plan = ui.client.post(f"{BASE}/backfill/preview", json={}).json()
    assert plan["papers"] == 2 and plan["estimated_usd"] is not None
    assert ui.harness.llm_calls("triage") == triaged  # 预览不花钱
    stale = ui.client.post(f"{BASE}/backfill", json={"plan": "stale"})
    assert stale.status_code == 422 and stale.json()["detail"] == "plan_changed"
    queued = ui.client.post(f"{BASE}/backfill", json={"plan": plan["plan"]})
    assert queued.status_code == 200 and queued.json() == {"queued": 2}
    assert run_pipeline(ui.runtime.snapshot()).ok
    assert ui.harness.llm_calls("triage") == triaged + 2


def test_backfill_needs_an_active_topic(ui):
    pause_topic(ui.runtime.snapshot(), TOPIC)
    response = ui.client.post(f"{BASE}/backfill/preview", json={})
    assert response.status_code == 422 and response.json()["detail"] == "topic_not_active"


def test_backfill_without_a_triage_model_asks_for_setup_without_queueing(harness: Harness):
    harness.write_papers([paper("alpha one")])
    assert run_pipeline(harness.ctx).ok
    harness.edit_yaml("llm.yaml", lambda data: data["tasks"].pop("triage"))
    harness.rebuild()
    ui = UI(harness)
    ui.client.put(f"{BASE}/profile", json=PROFILE)
    plan = ui.client.post(f"{BASE}/backfill/preview", json={}).json()
    assert plan["papers"] == 1
    response = ui.client.post(f"{BASE}/backfill", json={"plan": plan["plan"]})
    assert response.status_code == 409
    assert (response.json()["code"], response.json()["task"]) == (
        "model_configuration_required",
        "triage",
    )
    pending = ui.runtime.snapshot().store.jobs.list_by_status("pending")
    assert not [job for job in pending if job.stage == "triage"]


def test_saving_the_same_profile_twice_reports_no_change_the_second_time(ui):
    first = ui.client.put(f"{BASE}/profile", json=PROFILE).json()
    again = {**PROFILE, "goals": first["topic"]["goals"]}
    second = ui.client.put(f"{BASE}/profile", json=again)
    assert second.status_code == 200 and second.json()["changed"] is False
    assert [g["id"] for g in second.json()["topic"]["goals"]] == ["g1", "g2"]


def test_backfill_skips_papers_held_by_a_survey_and_says_so(ui, monkeypatch):
    ui.harness.write_papers([paper("alpha one"), paper("alpha two")])
    assert run_pipeline(ui.runtime.snapshot()).ok
    ui.client.put(f"{BASE}/profile", json=PROFILE)
    plan = ui.client.post(f"{BASE}/backfill/preview", json={}).json()
    assert plan["papers"] == 2 and plan["held"] == 0
    ctx = ui.runtime.snapshot()
    held_id = ctx.store.judgments.untriaged_papers(TOPIC, ctx.topics[TOPIC].topic_hash, None)[0]
    monkeypatch.setattr(
        type(ctx.store.surveys), "held_paper_ids", lambda _self: frozenset({held_id})
    )
    plan = ui.client.post(f"{BASE}/backfill/preview", json={}).json()
    assert plan["papers"] == 1 and plan["held"] == 1
    queued = ui.client.post(f"{BASE}/backfill", json={"plan": plan["plan"]})
    assert queued.json() == {"queued": 1}
    assert ctx.store.jobs.get(f"triage:{held_id}:{TOPIC}:{ctx.topics[TOPIC].topic_hash}") is None
