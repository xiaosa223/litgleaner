"""四种大模型协议的流式文本输出：请求格式、增量解析、用量与费用、错误分类。

事件流按各家官方文档的格式手写（2026-11 核对），不是真实录制：本机没有各家密钥。
"""

import json
from typing import Any

import httpx
import pytest
from tests.helpers.http import Router

from litgleaner.adapters.llm.anthropic import AnthropicMessagesLLM
from litgleaner.adapters.llm.base import HttpLLM
from litgleaner.adapters.llm.gemini import GeminiLLM
from litgleaner.adapters.llm.openai import OpenAIChatLLM, OpenAIResponsesLLM
from litgleaner.domain.errors import AuthFailed, PermanentError, TransientError
from litgleaner.ports.llm import StreamDone, TextDelta, TextRequest

KEY = "sk-test-secret"
PRICES = {"m1": [1.0, 4.0]}
REQUEST = TextRequest("translate", "m1", "系统提示", "<untrusted_paper>…</untrusted_paper>")
PIECES = ["注意力", "机制。\n", "第二句。"]


def _sse(events: list[tuple[str | None, Any]]) -> bytes:
    lines = []
    for name, data in events:
        if name:
            lines.append(f"event: {name}")
        lines.append(
            "data: " + (data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))
        )
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


def _anthropic(stop: str = "end_turn") -> bytes:
    events: list[tuple[str | None, Any]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "model": "m1-2026",
                    "usage": {"input_tokens": 1000, "output_tokens": 1},
                },
            },
        ),
        ("ping", {"type": "ping"}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
    ]
    events += [
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}},
        )
        for p in PIECES
    ]
    events += [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop},
                "usage": {"output_tokens": 500},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return _sse(events)


def _chat(finish: str = "stop", usage: bool = True) -> bytes:
    events: list[tuple[str | None, Any]] = [
        (
            None,
            {
                "model": "m1-2026",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            },
        )
    ]
    events += [
        (
            None,
            {
                "model": "m1-2026",
                "choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}],
            },
        )
        for p in PIECES
    ]
    events.append(
        (
            None,
            {"model": "m1-2026", "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        )
    )
    if usage:
        events.append(
            (
                None,
                {
                    "model": "m1-2026",
                    "choices": [],
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
                },
            )
        )
    events.append((None, "[DONE]"))
    return _sse(events)


def _responses(status: str = "completed") -> bytes:
    events: list[tuple[str | None, Any]] = [
        (
            "response.created",
            {"type": "response.created", "response": {"model": "m1-2026", "status": "in_progress"}},
        )
    ]
    events += [
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": p})
        for p in PIECES
    ]
    final = {
        "model": "m1-2026",
        "status": status,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }
    events.append((f"response.{status}", {"type": f"response.{status}", "response": final}))
    return _sse(events)


def _gemini(finish: str = "STOP") -> bytes:
    chunks = [
        {
            "candidates": [{"content": {"role": "model", "parts": [{"text": p}]}}],
            "modelVersion": "m1-001",
        }
        for p in PIECES
    ]
    chunks[-1]["candidates"][0]["finishReason"] = finish
    chunks[-1]["usageMetadata"] = {
        "promptTokenCount": 1000,
        "candidatesTokenCount": 300,
        "thoughtsTokenCount": 200,
    }
    return _sse([(None, c) for c in chunks])


CASES = {
    "anthropic": (AnthropicMessagesLLM, "/v1/messages", _anthropic),
    "chat": (OpenAIChatLLM, "/chat/completions", _chat),
    "responses": (OpenAIResponsesLLM, "/responses", _responses),
    "gemini": (GeminiLLM, "/models/m1:streamGenerateContent", _gemini),
}
SSE = {"content-type": "text/event-stream"}


def _make(cls: type[HttpLLM], router: Router) -> HttpLLM:
    return cls(
        "main",
        {"prices": PRICES, "base_url": "https://llm.test/v1"},
        KEY,
        transport=router.transport,
    )


def _run(llm: HttpLLM) -> tuple[list[str], StreamDone]:
    deltas: list[str] = []
    done: StreamDone | None = None
    for event in llm.stream(REQUEST):
        if isinstance(event, TextDelta):
            deltas.append(event.text)
        else:
            assert done is None, "StreamDone 只能出现一次，且在最后"
            done = event
    assert done is not None
    return deltas, done


@pytest.mark.parametrize("name", CASES)
def test_streams_text_in_order_then_usage_and_cost(name: str):
    cls, path, body = CASES[name]
    router = Router().reply(path, body=body(), headers=SSE)
    deltas, done = _run(_make(cls, router))

    assert "".join(deltas) == "".join(PIECES)
    assert (done.input_tokens, done.output_tokens) == (1000, 500)
    assert done.cost_usd == pytest.approx((1000 * 1.0 + 500 * 4.0) / 1_000_000)
    assert not done.estimated
    request = router.requests[-1]
    assert request.headers.get(
        "authorization", request.headers.get("x-api-key", request.headers.get("x-goog-api-key", ""))
    ).endswith(KEY)


def test_request_shapes_ask_for_a_stream_without_schema():
    sent: dict[str, Any] = {}
    for name, (cls, path, body) in CASES.items():
        router = Router().reply(path, body=body(), headers=SSE)
        _run(_make(cls, router))
        sent[name] = (str(router.requests[-1].url), json.loads(router.requests[-1].content))
    assert sent["anthropic"][1]["stream"] is True and "output_config" not in sent["anthropic"][1]
    assert sent["chat"][1]["stream"] is True
    assert sent["chat"][1]["stream_options"] == {"include_usage": True}
    assert "response_format" not in sent["chat"][1]
    assert sent["responses"][1]["stream"] is True and "text" not in sent["responses"][1]
    assert sent["gemini"][0].endswith(":streamGenerateContent?alt=sse")
    assert "responseSchema" not in json.dumps(sent["gemini"][1])


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("anthropic", _anthropic("max_tokens")),
        ("chat", _chat("length")),
        ("responses", _responses("incomplete")),
        ("gemini", _gemini("MAX_TOKENS")),
    ],
)
def test_truncated_stream_reports_usage_then_raises_transient(name: str, body: bytes):
    cls, path, _ = CASES[name]
    router = Router().reply(path, body=body, headers=SSE)
    events = []
    with pytest.raises(TransientError, match="截断"):
        for event in _make(cls, router).stream(REQUEST):
            events.append(event)
    done = [e for e in events if isinstance(e, StreamDone)]
    assert done and done[0].error is not None  # 失败也带着已知用量，网关据此记费用


def test_refusals_are_permanent():
    cases = [
        (AnthropicMessagesLLM, "/v1/messages", _anthropic("refusal")),
        (OpenAIChatLLM, "/chat/completions", _chat("content_filter")),
        (GeminiLLM, "/models/m1:streamGenerateContent", _gemini("SAFETY")),
    ]
    for cls, path, body in cases:
        router = Router().reply(path, body=body, headers=SSE)
        with pytest.raises(PermanentError):
            list(_make(cls, router).stream(REQUEST))


def test_error_event_mid_stream_is_transient():
    body = _sse(
        [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {"model": "m1", "usage": {"input_tokens": 10, "output_tokens": 1}},
                },
            ),
            (
                "error",
                {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
            ),
        ]
    )
    router = Router().reply("/v1/messages", body=body, headers=SSE)
    with pytest.raises(TransientError, match="Overloaded"):
        list(_make(AnthropicMessagesLLM, router).stream(REQUEST))


def test_http_errors_before_stream_are_classified_and_key_scrubbed():
    router = Router().reply(
        "/v1/messages", status=401, json_body={"error": {"message": f"bad key {KEY}"}}
    )
    with pytest.raises(AuthFailed) as caught:
        list(_make(AnthropicMessagesLLM, router).stream(REQUEST))
    assert KEY not in str(caught.value)


def test_missing_usage_is_estimated_not_silently_free():
    router = Router().reply("/chat/completions", body=_chat(usage=False), headers=SSE)
    _, done = _run(_make(OpenAIChatLLM, router))
    assert done.estimated
    assert done.input_tokens > 0 and done.output_tokens > 0
    assert done.cost_usd > 0


def test_connection_drop_mid_stream_is_transient():
    def handler(_: httpx.Request) -> httpx.Response:
        def chunks():
            yield b'data: {"choices":[{"index":0,"delta":{"content":"\\u534a"}}]}\n\n'
            raise httpx.ReadError("connection reset")

        return httpx.Response(200, headers=SSE, content=chunks())

    router = Router().on("/chat/completions", handler)
    with pytest.raises(TransientError):
        list(_make(OpenAIChatLLM, router).stream(REQUEST))


@pytest.mark.parametrize("name", CASES)
def test_stream_with_unknown_price_reports_tokens_and_unknown_cost(name: str):
    """ADR 0013：价格未知时照样流式输出，用量照报，费用为 None。"""
    cls, path, body = CASES[name]
    router = Router().reply(path, body=body(), headers=SSE)
    llm = cls("main", {"prices": {}}, KEY, transport=router.transport)
    _deltas, done = _run(llm)
    assert (done.input_tokens, done.output_tokens) == (1000, 500)
    assert done.cost_usd is None
