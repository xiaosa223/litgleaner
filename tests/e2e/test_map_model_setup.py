"""地图任务原地配置与其他模型任务隔离；协议请求全部由离线对端处理。"""

import pytest

from litgleaner.infra.config import load_config
from tests.e2e.test_model_settings import SETTINGS, connected, draft
from tests.e2e.test_model_settings import model_ui as mock_model_ui

model_ui = mock_model_ui

TASKS = {"triage", "extract", "assess", "translate", "ask", "map", "outline", "relate", "draft"}


def test_settings_advertise_unconfigured_map_without_disabling_reading(harness):
    (harness.root / "config/llm.yaml").unlink()
    harness.rebuild()
    ui = connected(harness)
    try:
        result = ui.client.get(SETTINGS)
        assert result.status_code == 200
        assert {item["task"] for item in result.json()["tasks"]} == TASKS
        assert all(not item["ready"] for item in result.json()["tasks"])
        assert ui.client.get("/api/inbox").status_code == 200
        assert harness.llm_calls() == 0 and ui.secrets.writes == []
    finally:
        ui.client.close()


def test_save_map_only_creates_reusable_map_route_without_model_calls(model_ui):
    ui = model_ui
    before = load_config(ui.harness.root / "config").llm.tasks
    response = ui.client.put(SETTINGS, json=draft(tasks=["map"]))
    assert response.status_code == 200, response.text
    assert {item["task"] for item in response.json()["tasks"] if item["ready"]} == {"map"}
    after = load_config(ui.harness.root / "config").llm.tasks
    assert all(after[name] == settings for name, settings in before.items())
    assert after["map"].prompt == "map" and after["map"].provider == "personal"
    ctx = ui.runtime.snapshot()
    assert "map" in ctx.llm.routes
    assert ui.peer.requests == [] and ui.harness.llm_calls() == 0
    ui.peer.structured = {"groups": []}
    result = ctx.llm.call(
        "map",
        {"output_language": "English"},
        '{"papers":[]}',
        {
            "type": "object",
            "required": ["groups"],
            "properties": {"groups": {"type": "array"}},
        },
    )
    assert result.data == {"groups": []}
    assert len(ui.peer.requests) == 1
    request = ui.peer.requests[0][1]
    assert request["messages"][0]["content"].find("English") >= 0
    assert ctx.store.runs.llm_cost_since("2026-01-01") == pytest.approx(0.00004)


def test_save_all_eight_tasks_in_one_request_and_reject_overlong_selection(model_ui):
    ui = model_ui
    result = ui.client.put(SETTINGS, json=draft(tasks=sorted(TASKS)))
    assert result.status_code == 200, result.text
    assert {item["task"] for item in result.json()["tasks"] if item["ready"]} == TASKS
    invalid = ui.client.put(SETTINGS, json=draft(tasks=[*sorted(TASKS), "map"]))
    assert invalid.status_code == 422
    assert ui.peer.requests == []
