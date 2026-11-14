"""列出提供方的可用模型（ADR 0013）：不计费、不猜价格，只读响应里明确的价格字段。

OpenAI 兼容样例是本机网关 /v1/models 的真实响应（2026-11-14 录制、裁剪字段）；
Anthropic、Gemini 样例按官方文档的列表结构手写。
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from litgleaner.adapters.llm.anthropic import AnthropicMessagesLLM
from litgleaner.adapters.llm.gemini import GeminiLLM
from litgleaner.adapters.llm.openai import OpenAIChatLLM, OpenAIResponsesLLM
from litgleaner.domain.errors import AuthFailed, SchemaChanged
from litgleaner.ports.llm import ModelInfo

FIXTURE = Path(__file__).parents[1] / "fixtures" / "llm_catalog" / "openai_compat_models.json"
KEY = "sk-catalog-secret"


def _serve(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("cls", [OpenAIChatLLM, OpenAIResponsesLLM])
def test_openai_compatible_lists_models_with_prices_per_million(cls: type) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=json.loads(FIXTURE.read_text(encoding="utf-8")))

    llm = cls("gw", {"base_url": "http://127.0.0.1:3425/v1"}, KEY, transport=_serve(handler))
    models = {model.id: model for model in llm.list_models()}

    assert str(seen[0].url) == "http://127.0.0.1:3425/v1/models"
    assert seen[0].method == "GET"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert models["labs/gpt-6-luna"] == ModelInfo("labs/gpt-6-luna", 0.1, 0.5)
    assert models["workbuddy-ai/hy3"] == ModelInfo("workbuddy-ai/hy3", 0.0, 0.0)
    # 没有价格字段：价格未知，不当作免费
    assert models["auto/deepseek-v4.1-flash"] == ModelInfo("auto/deepseek-v4.1-flash", None, None)


@pytest.mark.parametrize(
    "pricing",
    [
        {"prompt": "abc", "completion": "0.000001"},
        {"prompt": "-0.000001", "completion": "0.000001"},
        {"prompt": "0.000001"},
        {"prompt": True, "completion": "0.000001"},
        "free",
    ],
)
def test_malformed_or_partial_pricing_is_unknown(pricing: object) -> None:
    body = {"data": [{"id": "m", "pricing": pricing}]}
    llm = OpenAIChatLLM("gw", {}, KEY, transport=_serve(lambda _r: httpx.Response(200, json=body)))
    assert llm.list_models() == (ModelInfo("m", None, None),)


def test_anthropic_follows_pages_with_after_id() -> None:
    pages = {
        None: {
            "data": [{"type": "model", "id": "claude-a", "display_name": "A"}],
            "has_more": True,
            "last_id": "claude-a",
        },
        "claude-a": {
            "data": [{"type": "model", "id": "claude-b", "display_name": "B"}],
            "has_more": False,
            "last_id": "claude-b",
        },
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=pages[request.url.params.get("after_id")])

    llm = AnthropicMessagesLLM("a", {}, KEY, transport=_serve(handler))
    assert [m.id for m in llm.list_models()] == ["claude-a", "claude-b"]
    assert seen[0].url.path == "/v1/models"
    assert seen[0].headers["x-api-key"] == KEY
    assert all(m.input_price is None for m in llm.list_models())


def test_gemini_keeps_generate_content_models_and_strips_prefix() -> None:
    pages = {
        None: {
            "models": [
                {"name": "models/gemini-x", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/embed-x", "supportedGenerationMethods": ["embedContent"]},
            ],
            "nextPageToken": "p2",
        },
        "p2": {
            "models": [
                {"name": "models/gemini-y", "supportedGenerationMethods": ["generateContent"]}
            ]
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params.get("pageToken")])

    llm = GeminiLLM("g", {}, KEY, transport=_serve(handler))
    assert [m.id for m in llm.list_models()] == ["gemini-x", "gemini-y"]


def test_list_is_capped_and_deduplicated() -> None:
    body = {"data": [{"id": f"m{i % 1500}"} for i in range(3000)]}
    llm = OpenAIChatLLM("gw", {}, KEY, transport=_serve(lambda _r: httpx.Response(200, json=body)))
    models = llm.list_models()
    assert len(models) == 1000
    assert len({m.id for m in models}) == 1000


def test_endless_pagination_stops() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        token = request.url.params.get("pageToken", "0")
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": f"models/m{token}", "supportedGenerationMethods": ["generateContent"]}
                ],
                "nextPageToken": str(int(token) + 1),
            },
        )

    llm = GeminiLLM("g", {}, KEY, transport=_serve(handler))
    assert len(llm.list_models()) <= 50


def test_wrong_shape_is_schema_changed_and_auth_failure_is_classified() -> None:
    llm = OpenAIChatLLM(
        "gw", {}, KEY, transport=_serve(lambda _r: httpx.Response(200, json={"x": 1}))
    )
    with pytest.raises(SchemaChanged):
        llm.list_models()
    denied = OpenAIChatLLM("gw", {}, KEY, transport=_serve(lambda _r: httpx.Response(401)))
    with pytest.raises(AuthFailed):
        denied.list_models()
