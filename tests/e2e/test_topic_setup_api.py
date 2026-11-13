"""在领读器里新建课题：写主题文件、不启用、不调用模型，后续请求立即可见。"""

import pytest
from fastapi.testclient import TestClient

from litgleaner.application.topics import activate_topic
from litgleaner.infra.config import load_config
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from litgleaner.service import ModelRuntime
from tests.e2e.conftest import Harness
from tests.e2e.test_model_settings import MemorySecrets

BASE = "http://127.0.0.1:48151"
BODY = {"name": "EEG models", "background": "背景", "content": "内容", "goals": ["目标一"]}


def _client(harness, runtime):
    app = create_app(
        harness.ctx,
        LocalSecurity(48151, launch_token="topic-test"),
        None,
        context_provider=runtime.snapshot if runtime else (lambda: harness.ctx),
        model_settings=runtime,
        topic_settings=runtime,
    )
    client = TestClient(app, base_url=BASE)
    client.get("/launch", params={"token": "topic-test"})
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return client


@pytest.fixture
def topic_ui(harness: Harness):
    runtime = ModelRuntime(load_config(harness.root / "config"), harness.ctx, MemorySecrets())
    return harness, runtime, _client(harness, runtime)


def test_create_writes_an_inactive_topic_without_model_calls(topic_ui):
    harness, runtime, client = topic_ui
    created = client.post("/api/topics", json=BODY)
    assert created.status_code == 200, created.text
    assert created.json()["id"] == "eeg-models"
    assert (harness.root / "config" / "topics" / "eeg-models.yaml").is_file()
    assert "eeg-models" in load_config(harness.root / "config").topics
    assert harness.llm_calls() == 0
    assert runtime.snapshot().store.judgments.get_topic_state("eeg-models") is None
    listed = {item["id"]: item for item in client.get("/api/topics").json()}
    assert listed["eeg-models"]["status"] == "new" and listed["eeg-models"]["terms"] == 0
    assert listed["eeg-models"]["goals"] == [{"id": "g1", "text": "目标一"}]
    rounds = client.get("/api/topics/eeg-models/survey/rounds")
    assert rounds.status_code == 200 and rounds.json() == []


def test_list_reports_active_paused_and_query_size(topic_ui):
    _, runtime, client = topic_ui
    activate_topic(runtime.snapshot(), "alpha-topic")
    alpha = next(item for item in client.get("/api/topics").json() if item["id"] == "alpha-topic")
    assert (alpha["status"], alpha["terms"], alpha["name"]) == ("active", 1, "Alpha")


def test_same_name_twice_gets_two_topics(topic_ui):
    _, _, client = topic_ui
    ids = [client.post("/api/topics", json=BODY).json()["id"] for _ in range(2)]
    assert ids == ["eeg-models", "eeg-models-2"]


@pytest.mark.parametrize(
    "change",
    [{"background": " "}, {"goals": []}, {"name": "x" * 101}, {"extra": 1}, {"goals": ["  "]}],
)
def test_incomplete_profile_is_rejected_without_writing(topic_ui, change):
    harness, _, client = topic_ui
    response = client.post("/api/topics", json={**BODY, **change})
    assert response.status_code == 422
    assert sorted(p.name for p in (harness.root / "config" / "topics").iterdir()) == [
        "alpha-topic.yaml"
    ]


def test_create_requires_csrf(topic_ui):
    _, _, client = topic_ui
    del client.headers["X-CSRF-Token"]
    assert client.post("/api/topics", json=BODY).status_code == 403


def test_without_runtime_creation_is_unavailable_but_listing_works(harness):
    client = _client(harness, None)
    assert client.post("/api/topics", json=BODY).status_code == 422
    assert [item["id"] for item in client.get("/api/topics").json()] == ["alpha-topic"]


def test_unwritable_topic_directory_reports_a_topic_error(topic_ui, monkeypatch):
    _, runtime, client = topic_ui
    blocked = runtime._topics.directory / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(runtime._topics, "directory", blocked / "topics")
    response = client.post("/api/topics", json=BODY)
    assert response.status_code == 500 and response.json()["detail"] == "topic_save_failed"
    assert "eeg-models" not in runtime.snapshot().topics
