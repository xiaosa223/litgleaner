"""模型原地配置的 HTTP 验收：真实协议适配器，网络和钥匙串全部隔离。"""

import json
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from litgleaner import bootstrap
from litgleaner.adapters.llm.openai import OpenAIChatLLM
from litgleaner.infra import model_settings
from litgleaner.infra.config import load_config
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from litgleaner.service import ModelRuntime
from tests.e2e.conftest import Harness, paper

SECRET = "isolated-test-key-must-not-be-returned-2468"
BASE = "http://127.0.0.1:48129"
SETTINGS = "/api/settings/models"


class MemorySecrets:
    def __init__(self):
        self.values = {}
        self.writes = []
        self.fail_reads_after_write = False

    def get(self, name):
        if self.fail_reads_after_write and self.writes:
            raise RuntimeError(SECRET)
        return self.values.get(name)

    def set(self, name, value):
        self.writes.append(("set", name))
        self.values[name] = value

    def delete(self, name):
        self.writes.append(("delete", name))
        self.values.pop(name, None)


def draft(**changes):
    return {
        "provider": "personal",
        "protocol": "openai_chat",
        "model": "probe-model",
        "tasks": ["triage", "translate"],
        "input_price": 2.0,
        "output_price": 4.0,
        "api_key": SECRET,
        "base_url": "https://models.example.test/v1",
        "daily_budget_usd": 2.0,
        **changes,
    }


class ChatPeer:
    def __init__(self):
        self.requests = []
        self.structured = {"ok": True}
        self.status = 200
        self.content = None
        self.finish_reason = "stop"
        self.refusal = None

    def handle(self, request):
        body = json.loads(request.content)
        self.requests.append((request, body))
        assert request.method == "POST" and request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {SECRET}"
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"message": SECRET}})
        if not body.get("stream"):
            return httpx.Response(
                200,
                json={
                    "model": body["model"],
                    "choices": [
                        {
                            "message": {
                                "content": self.content or json.dumps(self.structured),
                                "refusal": self.refusal,
                            },
                            "finish_reason": self.finish_reason,
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                },
            )
        events = [
            {
                "model": body["model"],
                "choices": [
                    {"delta": {"content": "[[1]] 我们研究这个问题。"}, "finish_reason": None}
                ],
            },
            {"model": body["model"], "choices": [{"delta": {}, "finish_reason": "stop"}]},
            {
                "model": body["model"],
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        ]
        content = "".join(f"data: {json.dumps(item)}\n\n" for item in events) + "data: [DONE]\n\n"
        return httpx.Response(
            200,
            content=content.encode(),
            headers={
                "Content-Type": "text/event-stream",
            },
        )


def connected(harness, secrets=None):
    secrets = secrets or MemorySecrets()
    runtime = ModelRuntime(load_config(harness.root / "config"), harness.ctx, secrets)
    app = create_app(
        harness.ctx,
        LocalSecurity(48129, launch_token="test-launch"),
        None,
        context_provider=runtime.snapshot,
        model_settings=runtime,
    )
    client = TestClient(app, base_url=BASE)
    client.get("/launch", params={"token": "test-launch"})
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return SimpleNamespace(client=client, runtime=runtime, secrets=secrets, harness=harness)


@pytest.fixture
def model_ui(harness: Harness, monkeypatch):
    peer, clients = ChatPeer(), []

    def factory(args):
        key = args.secret(next(iter(args.credentials))) if args.credentials else None
        client = OpenAIChatLLM(
            args.name, args.options, key, transport=httpx.MockTransport(peer.handle)
        )
        clients.append(client)
        return client

    monkeypatch.setitem(bootstrap.LLMS, "openai_chat", factory)
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda *_a, **_kw: pytest.fail("模型设置测试禁止真实网络"),
    )
    ui = connected(harness)
    ui.peer = peer
    ui.clients = clients
    yield ui
    ui.client.close()
    for client in clients:
        client.close()


@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_empty_model_configuration_still_serves_settings(harness, missing):
    path = harness.root / "config" / "llm.yaml"
    path.unlink() if missing else path.write_text("{}\n", encoding="utf-8")
    harness.rebuild()
    ui = connected(harness)
    response = ui.client.get(SETTINGS)
    assert response.status_code == 200
    assert response.json()["providers"] == []
    assert all(not item["ready"] for item in response.json()["tasks"])
    assert ui.client.get("/api/inbox").status_code == 200
    assert ui.secrets.writes == [] and harness.llm_calls() == 0


def test_status_masks_legacy_credentials_and_endpoint_userinfo_query(harness):
    harness.edit_yaml(
        "llm.yaml",
        lambda data: data["providers"].update(
            {
                "legacy": {
                    "adapter": "openai_chat",
                    "enabled": False,
                    "credentials_env": ["LEGACY_KEY"],
                    "options": {
                        "base_url": "https://url-user:url-password@models.test/v1?token=url-query#f"
                    },
                }
            }
        ),
    )
    secrets = MemorySecrets()
    secrets.values["LEGACY_KEY"] = SECRET
    ui = connected(harness, secrets)
    response = ui.client.get(SETTINGS)
    assert response.status_code == 200
    provider = next(item for item in response.json()["providers"] if item["name"] == "legacy")
    assert provider["credential_set"] and provider["credential_last_four"] == SECRET[-4:]
    assert provider["base_url"] == "https://models.test/v1"
    assert all(
        value not in response.text for value in (SECRET, "url-user", "url-password", "url-query")
    )


def test_probe_exercises_structured_and_streaming_protocol_and_records_cost_only(model_ui):
    ui = model_ui
    path = ui.harness.root / "config" / "llm.yaml"
    previous = path.read_bytes()
    response = ui.client.post(SETTINGS + "/test", json=draft())
    assert response.status_code == 200 and response.json()["ok"]
    assert response.json()["capabilities"] == ["structured", "stream"]
    assert response.json()["cost_usd"] == pytest.approx(0.00008)
    assert len(ui.peer.requests) == 2
    structured, streamed = [body for _, body in ui.peer.requests]
    assert structured["response_format"]["type"] == "json_schema"
    assert streamed["stream"] and streamed["stream_options"]["include_usage"]
    assert all(body["max_completion_tokens"] == 128 for _, body in ui.peer.requests)
    assert ui.harness.llm_calls("configuration_test") == 2
    assert ui.harness.ctx.store.runs.llm_cost_since("2026-01-01") == pytest.approx(0.00008)
    assert path.read_bytes() == previous and not path.with_suffix(".yaml.bak").exists()
    assert ui.secrets.writes == [] and SECRET not in response.text
    assert all(client._http._client.is_closed for client in ui.clients)


def test_probe_invalid_structured_answer_is_rejected_but_known_usage_is_charged(model_ui):
    model_ui.peer.structured = {"ok": False}
    response = model_ui.client.post(SETTINGS + "/test", json=draft())
    assert response.status_code == 200 and not response.json()["ok"]
    assert response.json()["code"] == "model_connection_failed"
    assert response.json()["capabilities"] == []
    assert response.json()["cost_usd"] == pytest.approx(0.00004)
    assert len(model_ui.peer.requests) == 1
    assert model_ui.harness.llm_calls("configuration_test") == 1
    assert model_ui.secrets.writes == []


@pytest.mark.parametrize("failure", ["invalid_json", "truncated", "refused"])
def test_http_success_with_bad_model_output_still_records_known_usage(model_ui, failure):
    ui = model_ui
    if failure == "invalid_json":
        ui.peer.content = f"Invalid JSON {SECRET}"
    elif failure == "truncated":
        ui.peer.finish_reason = "length"
    else:
        ui.peer.refusal = SECRET
    response = ui.client.post(SETTINGS + "/test", json=draft())
    assert response.status_code == 200 and not response.json()["ok"]
    assert response.json()["code"] == "model_connection_failed"
    assert response.json()["cost_usd"] == pytest.approx(0.00004)
    assert ui.harness.ctx.store.runs.llm_cost_since("2026-01-01") == pytest.approx(0.00004)
    assert ui.harness.llm_calls("configuration_test") == 1
    assert len(ui.peer.requests) == 1 and ui.secrets.writes == []
    assert SECRET not in response.text
    assert all(client._http._client.is_closed for client in ui.clients)


def test_probe_provider_error_is_redacted_and_does_not_persist_draft(model_ui):
    model_ui.peer.status = 401
    response = model_ui.client.post(SETTINGS + "/test", json=draft())
    assert response.status_code == 200 and not response.json()["ok"]
    assert response.json()["code"] == "model_connection_failed"
    assert SECRET not in response.text and model_ui.secrets.writes == []
    assert len(model_ui.peer.requests) == 1
    assert all(client._http._client.is_closed for client in model_ui.clients)


def test_probe_stops_before_stream_when_structured_call_exhausts_budget(model_ui):
    ui = model_ui
    body = draft(daily_budget_usd=0.00003)
    assert ui.client.put(SETTINGS, json=body).status_code == 200
    response = ui.client.post(SETTINGS + "/test", json=body)
    assert response.status_code == 402
    assert response.json()["code"] == "budget_exceeded"
    assert len(ui.peer.requests) == 1 and not ui.peer.requests[0][1].get("stream")
    assert ui.harness.llm_calls("configuration_test") == 1
    assert ui.harness.ctx.store.runs.llm_cost_since("2026-01-01") == pytest.approx(0.00004)


def test_save_has_no_model_calls_enables_only_selected_tasks_and_reloads_next_request(model_ui):
    ui, harness = model_ui, model_ui.harness
    harness.write_papers([paper("Hot Model Switch", 5)])
    assert harness.run().ok
    paper_id = harness.ctx.store.judgments.papers_with_scores()[0]
    old = ui.runtime.snapshot()
    pending = old.llm.stream("translate", {}, "[[1]] old snapshot")
    ongoing = iter(pending)
    first = next(ongoing)
    before = harness.llm_calls()
    response = ui.client.put(SETTINGS, json=draft(tasks=["translate"]))
    assert response.status_code == 200 and not ui.peer.requests
    assert harness.llm_calls() == before
    ready = {item["task"] for item in response.json()["tasks"] if item["ready"]}
    assert ready == {"translate"}
    current = ui.runtime.snapshot()
    assert current is not old
    assert current.llm.routes["triage"].model == old.llm.routes["triage"].model
    assert current.llm.routes["triage"].prompt == old.llm.routes["triage"].prompt
    assert (
        current.llm.routes["triage"].provider_version == old.llm.routes["triage"].provider_version
    )
    assert old.llm.routes["translate"].model == "cheap"
    assert current.llm.routes["translate"].model == "probe-model"
    assert "old snapshot" in first + "".join(ongoing)
    assert pending.result.model == "cheap" and not ui.peer.requests
    translated = ui.client.post(f"/api/papers/{paper_id}/blocks/2/translate")
    assert '"verified": true' in translated.text and '"cached": false' in translated.text
    assert ui.peer.requests[-1][1]["model"] == "probe-model"
    assert old.llm.routes["translate"].model == "cheap"
    stored = (harness.root / "config" / "llm.yaml").read_text(encoding="utf-8")
    credential = yaml.safe_load(stored)["providers"]["personal"]["credentials_env"][0]
    assert ui.secrets.values[credential] == SECRET and SECRET not in stored


def test_successful_save_does_not_reread_keychain_after_commit(model_ui):
    ui = model_ui
    ui.secrets.fail_reads_after_write = True
    response = ui.client.put(SETTINGS, json=draft(tasks=["translate"]))
    assert response.status_code == 200
    provider = next(item for item in response.json()["providers"] if item["name"] == "personal")
    assert provider["credential_set"] and SECRET not in response.text
    assert ui.runtime.snapshot().llm.routes["translate"].model == "probe-model"
    path = ui.harness.root / "config" / "llm.yaml"
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    credential = saved["providers"]["personal"]["credentials_env"][0]
    assert ui.secrets.values[credential] == SECRET


def test_save_failure_returns_stable_error_and_keeps_running_snapshot(model_ui, monkeypatch):
    ui = model_ui
    old = ui.runtime.snapshot()
    path = ui.harness.root / "config" / "llm.yaml"
    previous = path.read_bytes()

    def fail_write(_path, _content):
        raise OSError(SECRET)

    monkeypatch.setattr(model_settings, "_atomic_write", fail_write)
    response = ui.client.put(SETTINGS, json=draft(tasks=["translate"]))
    assert response.status_code == 422 and response.json()["code"] == "configuration_error"
    assert SECRET not in response.text and ui.runtime.snapshot() is old
    assert path.read_bytes() == previous and ui.secrets.values == {}
    assert ui.peer.requests == []


def test_external_edit_returns_conflict_without_replacing_context_or_keys(model_ui):
    ui = model_ui
    old = ui.runtime.snapshot()
    path = ui.harness.root / "config" / "llm.yaml"
    external = path.read_bytes() + b"\n# edited outside the application\n"
    path.write_bytes(external)
    response = ui.client.put(SETTINGS, json=draft(tasks=["translate"]))
    assert response.status_code == 422
    assert response.json()["detail"] == "model_settings_changed_on_disk"
    assert path.read_bytes() == external and ui.runtime.snapshot() is old
    assert ui.secrets.writes == [] and ui.peer.requests == []


@pytest.mark.parametrize("method,path", [("post", SETTINGS + "/test"), ("put", SETTINGS)])
def test_model_mutations_require_csrf(model_ui, method, path):
    response = getattr(model_ui.client, method)(path, json=draft(), headers={"X-CSRF-Token": ""})
    assert response.status_code == 403
    assert model_ui.peer.requests == [] and model_ui.secrets.writes == []


@pytest.mark.parametrize(
    "change",
    [
        {"tasks": ["unimplemented_task"]},
        {"input_price": -1},
        {"api_key": ""},
        {"base_url": f"https://user:{SECRET}@models.test/v1?token={SECRET}"},
        {"unexpected": SECRET},
        {"protocol": "fake_llm"},
    ],
)
def test_invalid_settings_never_echo_raw_input_or_api_key(model_ui, change):
    response = model_ui.client.put(SETTINGS, json=draft(**change))
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert SECRET not in response.text and "input" not in response.json()
    assert model_ui.peer.requests == [] and model_ui.secrets.writes == []
