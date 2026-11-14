"""列出可用模型与价格未知的保存（ADR 0013）：HTTP 验收，网络和钥匙串全部隔离。"""

import json
from pathlib import Path

import httpx
import pytest
import yaml

from litgleaner import bootstrap
from litgleaner.adapters.llm.openai import OpenAIChatLLM
from tests.e2e.test_model_settings import SECRET, SETTINGS, ChatPeer, connected, draft

LIST = f"{SETTINGS}/list"
FIXTURE = Path(__file__).parents[1] / "fixtures" / "llm_catalog" / "openai_compat_models.json"


class CatalogPeer(ChatPeer):
    def __init__(self) -> None:
        super().__init__()
        self.list_requests: list[httpx.Request] = []
        self.list_status = 200

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.list_requests.append(request)
            assert request.url.path == "/v1/models"
            assert request.headers["Authorization"] == f"Bearer {SECRET}"
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={"error": {"message": SECRET}})
            return httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))
        return super().handle(request)


@pytest.fixture
def catalog_ui(harness, monkeypatch):
    peer = CatalogPeer()

    def factory(args):
        key = args.secret(next(iter(args.credentials))) if args.credentials else None
        return OpenAIChatLLM(
            args.name, args.options, key, transport=httpx.MockTransport(peer.handle)
        )

    monkeypatch.setitem(bootstrap.LLMS, "openai_chat", factory)
    monkeypatch.setattr(
        httpx.HTTPTransport,
        "handle_request",
        lambda *_a, **_kw: pytest.fail("模型设置测试禁止真实网络"),
    )
    ui = connected(harness)
    ui.peer = peer
    yield ui
    ui.client.close()


def _source(**changes):
    return {
        "provider": "personal",
        "protocol": "openai_chat",
        "api_key": SECRET,
        "base_url": "https://models.example.test/v1",
        **changes,
    }


def test_list_returns_models_with_known_or_unknown_prices_and_writes_nothing(catalog_ui):
    llm_file = catalog_ui.harness.root / "config" / "llm.yaml"
    before = llm_file.read_bytes()
    response = catalog_ui.client.post(LIST, json=_source())
    assert response.status_code == 200
    models = {item["id"]: item for item in response.json()["models"]}
    assert models["labs/gpt-6-luna"] == {
        "id": "labs/gpt-6-luna",
        "input_price": 0.1,
        "output_price": 0.5,
    }
    assert models["auto/deepseek-v4.1-flash"]["input_price"] is None
    assert SECRET not in response.text
    assert llm_file.read_bytes() == before
    assert catalog_ui.secrets.writes == []
    assert catalog_ui.peer.requests == []  # 没有调用生成接口


def test_list_reuses_saved_key_when_draft_omits_it(catalog_ui):
    saved = catalog_ui.client.put(SETTINGS, json=draft(base_url="https://models.example.test/v1"))
    assert saved.status_code == 200
    response = catalog_ui.client.post(LIST, json=_source(api_key=None))
    assert response.status_code == 200
    assert catalog_ui.peer.list_requests[-1].headers["Authorization"] == f"Bearer {SECRET}"


def test_list_failure_is_a_fixed_code_without_echoing_provider_error(catalog_ui):
    catalog_ui.peer.list_status = 401
    response = catalog_ui.client.post(LIST, json=_source())
    assert response.status_code == 200
    assert response.json() == {"models": [], "code": "model_list_failed"}
    assert SECRET not in response.text


def test_unknown_protocol_is_rejected(catalog_ui):
    response = catalog_ui.client.post(LIST, json=_source(protocol="fake_llm"))
    assert response.status_code in {400, 422}


def test_saving_without_price_stores_null_and_task_is_ready(catalog_ui):
    body = draft(input_price=None, output_price=None, tasks=["triage"])
    response = catalog_ui.client.put(SETTINGS, json=body)
    assert response.status_code == 200, response.text
    triage = next(item for item in response.json()["tasks"] if item["task"] == "triage")
    assert triage["ready"] is True
    saved = yaml.safe_load(
        (catalog_ui.harness.root / "config" / "llm.yaml").read_text(encoding="utf-8")
    )
    assert saved["providers"]["personal"]["options"]["prices"]["probe-model"] is None


def test_probe_without_price_reports_unknown_estimate_and_cost(catalog_ui):
    body = draft(input_price=None, output_price=None, tasks=["triage"])
    response = catalog_ui.client.post(f"{SETTINGS}/test", json=body)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is True
    assert result["estimated_usd"] is None
    assert result["cost_usd"] is None
