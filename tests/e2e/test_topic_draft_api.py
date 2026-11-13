"""起草检索式（第 7.2 节）：先预览、确认后调用一次模型；草稿不自动生效，确认合并后才写入并启用。"""

import pytest
import yaml
from fastapi.testclient import TestClient

from litgleaner.application.pipeline import run_on_demand
from litgleaner.application.topics import pause_topic
from litgleaner.domain.errors import PermanentError
from litgleaner.infra import topic_files
from litgleaner.infra.config import load_config
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from litgleaner.service import ModelRuntime
from tests.e2e.conftest import Harness
from tests.e2e.test_model_settings import MemorySecrets

TOPIC = "alpha-topic"
BASE = f"/api/topics/{TOPIC}/draft"
QUERY = {
    "include": [{"term": "EEG foundation model", "synonyms": ["brain foundation model"]}],
    "exclude": ["fMRI"],
    "glossary": [
        {
            "forms": ["foundation model", "FM"],
            "translation": "基础模型",
            "definition": "大规模预训练模型",
        }
    ],
}


def _with_draft_route(data):
    data["tasks"]["draft"] = {"provider": "main", "model": "strong", "prompt": "draft"}


class UI:
    def __init__(self, harness: Harness):
        self.harness = harness
        self.runtime = ModelRuntime(
            load_config(harness.root / "config"), harness.ctx, MemorySecrets()
        )
        app = create_app(
            harness.ctx,
            LocalSecurity(48152, launch_token="draft-test"),
            None,
            context_provider=self.runtime.snapshot,
            model_settings=self.runtime,
            topic_settings=self.runtime,
        )
        self.client = TestClient(app, base_url="http://127.0.0.1:48152")
        self.client.get("/launch", params={"token": "draft-test"})
        self.client.headers["X-CSRF-Token"] = self.client.get("/api/session").json()["csrf_token"]

    def preview(self):
        response = self.client.post(f"{BASE}/preview", json={})
        assert response.status_code == 200, response.text
        return response.json()

    def generate(self):
        plan = self.preview()
        queued = self.client.post(BASE, json={"plan_hash": plan["plan_hash"]})
        assert queued.status_code == 200, queued.text
        report = run_on_demand(self.runtime.snapshot())
        assert report.ok, report.summary()
        done = self.client.get(f"{BASE}/jobs/{queued.json()['job_key']}")
        assert done.status_code == 200 and done.json()["status"] == "done", done.text
        return plan, done.json()


@pytest.fixture
def ui(harness: Harness) -> UI:
    harness.edit_yaml("llm.yaml", _with_draft_route)
    harness.rebuild()
    return UI(harness)


def test_preview_calls_no_model_and_reports_unknown_cost(ui):
    plan = ui.preview()
    assert plan["estimated_usd"] is None and plan["cached"] is False
    assert plan["current"]["include"] == ["alpha"]
    assert ui.harness.llm_calls() == 0
    assert ui.client.get(BASE).json()["draft"] is None


def test_generate_writes_a_draft_file_with_reasons_and_never_changes_the_topic(ui):
    _, done = ui.generate()
    draft = done["draft"]
    assert ui.harness.llm_calls("draft") == 1
    assert draft["include"][0]["term"] and draft["include"][0]["reason"]
    assert draft["glossary"][0]["translation"]
    path = ui.harness.root / "config" / "topics" / f"{TOPIC}.draft.yaml"
    assert path.is_file()
    assert load_config(ui.harness.root / "config").topics[TOPIC].query.include == ("alpha",)
    view = ui.client.get(BASE).json()
    assert view["draft"]["plan_hash"] == done["draft"]["plan_hash"] and view["stale"] is False


def test_same_plan_twice_reuses_the_draft_without_another_call(ui):
    ui.generate()
    plan = ui.preview()
    assert plan["cached"] is True and plan["estimated_usd"] == 0
    queued = ui.client.post(BASE, json={"plan_hash": plan["plan_hash"]})
    assert queued.json()["status"] == "done"
    run_on_demand(ui.runtime.snapshot())
    assert ui.harness.llm_calls("draft") == 1


def test_stale_plan_is_rejected(ui):
    plan = ui.preview()
    response = ui.client.post(BASE, json={"plan_hash": plan["plan_hash"] + "x"})
    assert response.status_code == 422
    assert ui.harness.llm_calls() == 0


def test_missing_model_asks_for_setup_without_queueing(harness):
    page = UI(harness)
    plan = page.preview()
    response = page.client.post(BASE, json={"plan_hash": plan["plan_hash"]})
    assert response.status_code == 409 and response.json()["task"] == "draft"


def test_apply_writes_query_and_glossary_then_activates(ui):
    before = (ui.harness.root / "config" / "topics" / f"{TOPIC}.yaml").read_text(encoding="utf-8")
    response = ui.client.put(f"/api/topics/{TOPIC}/query", json=QUERY)
    assert response.status_code == 200, response.text
    root = ui.harness.root / "config" / "topics"
    saved = yaml.safe_load((root / f"{TOPIC}.yaml").read_text(encoding="utf-8"))
    assert saved["query"] == {
        "include": ["EEG foundation model"],
        "synonyms": {"EEG foundation model": ["brain foundation model"]},
        "exclude": ["fMRI"],
    }
    assert (root / f"{TOPIC}.yaml.bak").read_text(encoding="utf-8") == before
    config = load_config(ui.harness.root / "config")
    assert config.glossaries[TOPIC].terms[0].translation == "基础模型"
    snapshot = ui.runtime.snapshot()
    assert snapshot.topics[TOPIC].query.include == ("EEG foundation model",)
    assert snapshot.glossaries[TOPIC].terms[0].forms == ("foundation model", "FM")
    assert snapshot.store.judgments.get_topic_state(TOPIC).status == "active"
    listed = next(t for t in ui.client.get("/api/topics").json() if t["id"] == TOPIC)
    assert (listed["status"], listed["terms"]) == ("active", 2)
    assert ui.harness.llm_calls() == 0


def test_after_apply_the_previous_draft_is_still_shown_for_the_same_profile(ui):
    ui.generate()
    ui.client.put(f"/api/topics/{TOPIC}/query", json=QUERY)
    assert ui.client.get(BASE).json()["stale"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"include": []},
        {"include": [{"term": " ", "synonyms": []}]},
        {"exclude": ["EEG foundation model"]},
        {"glossary": [{"forms": ["x"], "translation": ""}]},
        {"glossary": [{"forms": [" "], "translation": "y"}]},
        {"glossary": [{"forms": [], "translation": "y"}]},
    ],
)
def test_invalid_query_is_rejected_without_writing(ui, change):
    path = ui.harness.root / "config" / "topics" / f"{TOPIC}.yaml"
    before = path.read_text(encoding="utf-8")
    response = ui.client.put(f"/api/topics/{TOPIC}/query", json={**QUERY, **change})
    assert response.status_code == 422
    assert path.read_text(encoding="utf-8") == before


def test_apply_refuses_when_the_file_was_edited_by_hand(ui):
    ui.harness.edit_yaml(f"topics/{TOPIC}.yaml", lambda data: data.update(name="改过"))
    response = ui.client.put(f"/api/topics/{TOPIC}/query", json=QUERY)
    assert response.status_code == 422 and response.json()["detail"] == "topic_changed_on_disk"
    assert ui.runtime.snapshot().topics[TOPIC].query.include == ("alpha",)
    assert not (ui.harness.root / "config" / "topics" / f"{TOPIC}.glossary.yaml").exists()


def test_unknown_topic_is_404(ui):
    assert ui.client.post("/api/topics/nope/draft/preview", json={}).status_code == 404
    assert ui.client.put("/api/topics/nope/query", json=QUERY).status_code == 404


def test_new_topic_becomes_active_only_after_the_query_is_confirmed(ui):
    body = {"name": "EEG", "background": "背景", "content": "内容", "goals": ["目标"]}
    topic_id = ui.client.post("/api/topics", json=body).json()["id"]
    store = ui.runtime.snapshot().store
    assert store.judgments.get_topic_state(topic_id) is None
    preview = ui.client.post(f"/api/topics/{topic_id}/draft/preview", json={}).json()
    assert preview["current"]["include"] == []
    assert ui.client.put(f"/api/topics/{topic_id}/query", json=QUERY).status_code == 200
    assert store.judgments.get_topic_state(topic_id).status == "active"


def test_saving_keeps_a_paused_topic_paused(ui):
    pause_topic(ui.runtime.snapshot(), TOPIC)
    response = ui.client.put(f"/api/topics/{TOPIC}/query", json=QUERY)
    assert response.status_code == 200 and response.json()["status"] == "paused"
    assert ui.runtime.snapshot().store.judgments.get_topic_state(TOPIC).status == "paused"


def test_existing_glossary_is_backed_up_and_restored_when_the_topic_write_fails(ui, monkeypatch):
    root = ui.harness.root / "config" / "topics"
    (root / f"{TOPIC}.glossary.yaml").write_text(
        "target_lang: zh-CN\nterms: []\n", encoding="utf-8"
    )
    ui.harness.rebuild()
    page = UI(ui.harness)
    real = topic_files._replace

    def fail_topic(path, content):
        if path.name == f"{TOPIC}.yaml":
            raise OSError("disk full")
        real(path, content)

    monkeypatch.setattr(topic_files, "_replace", fail_topic)
    response = page.client.put(f"/api/topics/{TOPIC}/query", json=QUERY)
    assert response.status_code == 500 and response.json()["detail"] == "topic_save_failed"
    assert (root / f"{TOPIC}.glossary.yaml").read_text(
        encoding="utf-8"
    ) == "target_lang: zh-CN\nterms: []\n"
    assert page.runtime.snapshot().topics[TOPIC].query.include == ("alpha",)


def test_dead_draft_job_can_be_confirmed_again(ui):
    ui.harness.ctx.llm.routes["draft"].llm.fail_tasks["draft"] = [PermanentError("boom")]
    plan = ui.preview()
    first = ui.client.post(BASE, json={"plan_hash": plan["plan_hash"]}).json()
    run_on_demand(ui.runtime.snapshot())
    assert ui.client.get(f"{BASE}/jobs/{first['job_key']}").json()["status"] == "dead"
    again = ui.client.post(BASE, json={"plan_hash": plan["plan_hash"]}).json()
    assert again["status"] == "pending"
    run_on_demand(ui.runtime.snapshot())
    assert ui.client.get(f"{BASE}/jobs/{first['job_key']}").json()["status"] == "done"


def test_superseded_plan_fails_without_calling_the_model(ui):
    old = ui.preview()
    queued = ui.client.post(BASE, json={"plan_hash": old["plan_hash"]}).json()
    ui.client.put(f"/api/topics/{TOPIC}/query", json=QUERY)  # 当前检索式变了，旧计划作废
    run_on_demand(ui.runtime.snapshot())
    status = ui.client.get(f"{BASE}/jobs/{queued['job_key']}").json()
    assert status["status"] == "dead" and ui.harness.llm_calls("draft") == 0
