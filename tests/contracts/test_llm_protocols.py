"""四种大模型协议适配器：请求格式、响应解析、错误分类。

响应样例按各家官方文档手写（2026-11 核对），不是真实录制；真实连通性要用 triage_eval 冒烟测试验证。
"""

import json
from collections.abc import Mapping
from typing import Any

import pytest
from tests.helpers.http import Router

from litgleaner.adapters.llm.anthropic import AnthropicMessagesLLM
from litgleaner.adapters.llm.base import HttpLLM, relax_schema
from litgleaner.adapters.llm.gemini import GeminiLLM
from litgleaner.adapters.llm.openai import OpenAIChatLLM, OpenAIResponsesLLM
from litgleaner.domain.errors import (
    AuthFailed,
    ConfigError,
    PermanentError,
    RateLimited,
    TransientError,
)
from litgleaner.ports.llm import LLMRequest

KEY = "sk-test-secret"
SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relevant", "reason"],
    "properties": {
        "relevant": {"type": "boolean"},
        "reason": {"type": "string", "maxLength": 300},
    },
}
ANSWER = {"relevant": True, "reason": "直接相关"}
REQUEST = LLMRequest("triage", "m1", "系统提示", "<untrusted_paper>…</untrusted_paper>", SCHEMA)
PRICES = {"m1": [1.0, 4.0]}


def _anthropic_body(text: str, stop: str = "end_turn") -> dict[str, Any]:
    return {
        "model": "m1-20261001",
        "stop_reason": stop,
        "content": [{"type": "thinking", "thinking": "…"}, {"type": "text", "text": text}],
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }


def _chat_body(content: str | None, finish: str = "stop", refusal: str | None = None) -> dict:
    message = {"role": "assistant", "content": content, "refusal": refusal}
    return {
        "model": "m1-2026",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
    }


def _responses_body(parts: list[dict[str, Any]], status: str = "completed") -> dict[str, Any]:
    return {
        "model": "m1-2026",
        "status": status,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "output": [
            {"type": "reasoning", "summary": []},
            {"type": "message", "role": "assistant", "content": parts},
        ],
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }


def _gemini_body(text: str, finish: str = "STOP") -> dict[str, Any]:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": "思考过程", "thought": True}, {"text": text}],
                },
                "finishReason": finish,
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 1000,
            "candidatesTokenCount": 300,
            "thoughtsTokenCount": 200,
        },
        "modelVersion": "m1-001",
    }


CASES = {
    "anthropic": (AnthropicMessagesLLM, "/v1/messages", _anthropic_body(json.dumps(ANSWER))),
    "chat": (OpenAIChatLLM, "/chat/completions", _chat_body(json.dumps(ANSWER))),
    "responses": (
        OpenAIResponsesLLM,
        "/responses",
        _responses_body([{"type": "output_text", "text": json.dumps(ANSWER)}]),
    ),
    "gemini": (GeminiLLM, "/models/m1:generateContent", _gemini_body(json.dumps(ANSWER))),
}


def _make(cls: type[HttpLLM], router: Router, **options: Any) -> HttpLLM:
    merged = {"prices": PRICES, "base_url": "https://llm.test/v1", **options}
    return cls("main", merged, KEY, transport=router.transport)


def _sent(router: Router) -> dict[str, Any]:
    return json.loads(router.requests[-1].content)


@pytest.mark.parametrize("name", list(CASES))
def test_returns_parsed_answer_with_tokens_and_cost(name: str):
    cls, path, body = CASES[name]
    router = Router().reply(path, json_body=body)
    response = _make(cls, router).complete(REQUEST)
    assert dict(response.data) == ANSWER
    assert (response.input_tokens, response.output_tokens) == (1000, 500)
    assert response.cost_usd == pytest.approx((1000 * 1.0 + 500 * 4.0) / 1e6)
    assert response.model.startswith("m1")
    assert str(router.requests[-1].url).startswith("https://llm.test/v1/")


@pytest.mark.parametrize("name", list(CASES))
def test_sends_key_only_in_auth_header_and_schema_without_length_limits(name: str):
    cls, path, body = CASES[name]
    router = Router().reply(path, json_body=body)
    _make(cls, router).complete(REQUEST)
    request = router.requests[-1]
    assert KEY not in str(request.url)
    assert any(KEY in value for value in request.headers.values())
    sent = request.content.decode()
    assert '"relevant"' in sent
    assert "maxLength" not in sent  # 三家的结构化输出都不支持或忽略长度约束
    assert "系统提示" in sent and "untrusted_paper" in sent


def test_anthropic_request_shape():
    router = Router().reply("/v1/messages", json_body=CASES["anthropic"][2])
    _make(AnthropicMessagesLLM, router, max_output_tokens=1234).complete(REQUEST)
    body, headers = _sent(router), router.requests[-1].headers
    assert headers["x-api-key"] == KEY and headers["anthropic-version"] == "2023-06-01"
    assert body["system"] == "系统提示" and body["max_tokens"] == 1234
    assert body["messages"] == [{"role": "user", "content": REQUEST.user}]
    assert body["output_config"]["format"]["type"] == "json_schema"


def test_openai_chat_request_shape():
    router = Router().reply("/chat/completions", json_body=CASES["chat"][2])
    _make(OpenAIChatLLM, router).complete(REQUEST)
    body = _sent(router)
    assert router.requests[-1].headers["authorization"] == f"Bearer {KEY}"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    fmt = body["response_format"]
    assert fmt["type"] == "json_schema" and fmt["json_schema"]["strict"] is True
    assert fmt["json_schema"]["name"] == "triage" and "max_completion_tokens" in body


def test_openai_responses_request_shape():
    router = Router().reply("/responses", json_body=CASES["responses"][2])
    _make(OpenAIResponsesLLM, router).complete(REQUEST)
    body = _sent(router)
    assert body["instructions"] == "系统提示" and body["input"] == REQUEST.user
    assert body["text"]["format"]["type"] == "json_schema" and body["store"] is False


def test_gemini_request_shape():
    router = Router().reply(":generateContent", json_body=CASES["gemini"][2])
    _make(GeminiLLM, router).complete(REQUEST)
    body = _sent(router)
    assert router.requests[-1].headers["x-goog-api-key"] == KEY
    assert body["systemInstruction"] == {"parts": [{"text": "系统提示"}]}
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json" and "responseJsonSchema" in config
    assert config["maxOutputTokens"] == 4000 and "maxOutputTokens" not in body


def test_default_base_urls_point_at_official_endpoints():
    expected = {
        AnthropicMessagesLLM: "https://api.anthropic.com/v1/messages",
        OpenAIChatLLM: "https://api.openai.com/v1/chat/completions",
        OpenAIResponsesLLM: "https://api.openai.com/v1/responses",
        GeminiLLM: "https://generativelanguage.googleapis.com/v1beta/models/m1:generateContent",
    }
    for cls, url in expected.items():
        name = next(k for k, v in CASES.items() if v[0] is cls)
        router = Router().reply("", json_body=CASES[name][2])
        cls("main", {"prices": PRICES}, KEY, transport=router.transport).complete(REQUEST)
        assert str(router.requests[-1].url) == url


def test_extra_body_is_deep_merged_without_dropping_schema():
    router = Router().reply("/v1/messages", json_body=CASES["anthropic"][2])
    llm = _make(AnthropicMessagesLLM, router, extra_body={"output_config": {"effort": "low"}})
    llm.complete(REQUEST)
    config = _sent(router)["output_config"]
    assert config["effort"] == "low" and config["format"]["type"] == "json_schema"


def test_prompt_mode_puts_schema_in_system_and_accepts_fenced_json():
    fenced = "```json\n" + json.dumps(ANSWER, ensure_ascii=False) + "\n```"
    router = Router().reply("/chat/completions", json_body=_chat_body(fenced))
    response = _make(OpenAIChatLLM, router, structured="prompt").complete(REQUEST)
    body = _sent(router)
    assert "response_format" not in body
    assert "maxLength" in body["messages"][0]["content"]  # 提示词里给完整结构
    assert dict(response.data) == ANSWER


@pytest.mark.parametrize(
    ("cls", "path", "body"),
    [
        (AnthropicMessagesLLM, "/v1/messages", _anthropic_body("", stop="refusal")),
        (OpenAIChatLLM, "/chat/completions", _chat_body(None, refusal="不能回答")),
        (OpenAIResponsesLLM, "/responses", _responses_body([{"type": "refusal", "refusal": "否"}])),
        (GeminiLLM, ":generateContent", _gemini_body("", finish="SAFETY")),
        (GeminiLLM, ":generateContent", {"promptFeedback": {"blockReason": "SAFETY"}}),
    ],
)
def test_refusal_is_permanent(cls: type[HttpLLM], path: str, body: dict[str, Any]):
    router = Router().reply(path, json_body=body)
    with pytest.raises(PermanentError, match="拒绝"):
        _make(cls, router).complete(REQUEST)


@pytest.mark.parametrize(
    ("cls", "path", "body"),
    [
        (AnthropicMessagesLLM, "/v1/messages", _anthropic_body('{"rel', stop="max_tokens")),
        (OpenAIChatLLM, "/chat/completions", _chat_body('{"rel', finish="length")),
        (
            OpenAIResponsesLLM,
            "/responses",
            _responses_body([{"type": "output_text", "text": '{"rel'}], status="incomplete"),
        ),
        (GeminiLLM, ":generateContent", _gemini_body('{"rel', finish="MAX_TOKENS")),
    ],
)
def test_truncated_output_is_transient_and_mentions_limit(
    cls: type[HttpLLM], path: str, body: dict[str, Any]
):
    router = Router().reply(path, json_body=body)
    with pytest.raises(TransientError, match="max_output_tokens"):
        _make(cls, router).complete(REQUEST)


def test_non_json_text_is_transient():
    router = Router().reply("/chat/completions", json_body=_chat_body("我认为相关"))
    with pytest.raises(TransientError, match="JSON"):
        _make(OpenAIChatLLM, router).complete(REQUEST)


def test_http_errors_are_classified_and_key_is_scrubbed():
    router = Router().reply("/v1/messages", 401, body=f"bad key {KEY}".encode())
    with pytest.raises(AuthFailed) as error:
        _make(AnthropicMessagesLLM, router).complete(REQUEST)
    assert KEY not in str(error.value)
    router = Router().reply("/v1/messages", 429, json_body={}, headers={"retry-after": "30"})
    with pytest.raises(RateLimited) as limited:
        _make(AnthropicMessagesLLM, router).complete(REQUEST)
    assert limited.value.retry_after_seconds == 30
    router = Router().reply("/v1/messages", 529, json_body={"type": "overloaded_error"})
    with pytest.raises(TransientError):
        _make(AnthropicMessagesLLM, router).complete(REQUEST)


def test_unexpected_response_shape_is_schema_changed():
    router = Router().reply("/chat/completions", json_body={"choices": []})
    with pytest.raises(PermanentError, match="结构"):
        _make(OpenAIChatLLM, router).complete(REQUEST)


def test_without_key_no_auth_header_is_sent():
    router = Router().reply("/chat/completions", json_body=CASES["chat"][2])
    OpenAIChatLLM("local", {"prices": PRICES}, None, transport=router.transport).complete(REQUEST)
    assert "authorization" not in router.requests[-1].headers


def test_model_without_price_is_allowed_and_billed_as_unknown():
    """ADR 0013：缺价格或价格为 null 表示未知，照常调用，费用记为 None 而不是 0。"""
    for prices in ({}, {"m1": None}):
        router = Router().reply("/chat/completions", json_body=CASES["chat"][2])
        llm = OpenAIChatLLM("main", {"prices": prices}, KEY, transport=router.transport)
        assert llm.price("m1") is None
        response = llm.complete(REQUEST)
        assert response.cost_usd is None
        assert response.input_tokens > 0


def test_invalid_options_fail_at_startup():
    with pytest.raises(ConfigError):
        OpenAIChatLLM("main", {"prices": PRICES, "unknown": 1}, KEY)
    with pytest.raises(ConfigError):
        OpenAIChatLLM("main", {"prices": {"m1": [-1, 0]}}, KEY)


def test_relax_schema_strips_unsupported_keywords_but_keeps_property_names():
    schema = {
        "type": "object",
        "properties": {
            "maximum": {"type": ["integer", "null"], "minimum": 0, "maximum": 5},
            "items": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 9}},
        },
        "required": ["maximum", "items"],
    }
    relaxed = relax_schema(schema)
    assert relaxed["properties"]["maximum"] == {"type": ["integer", "null"]}
    assert relaxed["properties"]["items"] == {"type": "array", "items": {"type": "string"}}
    assert relaxed["required"] == ["maximum", "items"]
    assert schema["properties"]["maximum"]["maximum"] == 5  # 不修改原对象
