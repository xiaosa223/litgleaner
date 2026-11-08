"""推理模型先思考再给正文：流里先出一次“思考中”事件，思考内容不混进正文（第 15.2 节）。

OpenAI Chat 用真实录制（本机 OpenAI 兼容网关、免费推理模型，2026-11-08）；
其余三种按官方文档的事件格式手写。
"""

import json
from pathlib import Path
from typing import Any

import pytest
from tests.helpers.http import Router

from litgleaner.adapters.llm.anthropic import AnthropicMessagesLLM
from litgleaner.adapters.llm.base import HttpLLM
from litgleaner.adapters.llm.gemini import GeminiLLM
from litgleaner.adapters.llm.openai import OpenAIChatLLM, OpenAIResponsesLLM
from litgleaner.ports.llm import StreamDone, TextDelta, TextRequest, Thinking

FIXTURES = Path(__file__).parents[1] / "fixtures"
REQUEST = TextRequest("translate", "m1", "系统提示", "<untrusted_paper>…</untrusted_paper>")
SSE = {"content-type": "text/event-stream"}
SECRET_THOUGHT = "这是模型的思考"


def _sse(events: list[tuple[str | None, Any]]) -> bytes:
    lines: list[str] = []
    for name, data in events:
        lines += [f"event: {name}"] if name else []
        lines += ["data: " + json.dumps(data, ensure_ascii=False), ""]
    return ("\n".join(lines) + "\n").encode()


def _anthropic() -> bytes:
    def delta(index: int, payload: dict[str, Any]) -> tuple[str, Any]:
        return "content_block_delta", {"type": "content_block_delta", "index": index, **payload}

    return _sse(
        [
            (
                "message_start",
                {"type": "message_start", "message": {"model": "m1", "usage": {"input_tokens": 9}}},
            ),
            (
                "content_block_start",
                {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
            ),
            delta(0, {"delta": {"type": "thinking_delta", "thinking": SECRET_THOUGHT}}),
            delta(0, {"delta": {"type": "signature_delta", "signature": "sig"}}),
            delta(1, {"delta": {"type": "text_delta", "text": "译文。"}}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 5},
                },
            ),
        ]
    )


def _responses() -> bytes:
    return _sse(
        [
            (
                "response.reasoning_summary_text.delta",
                {"type": "response.reasoning_summary_text.delta", "delta": SECRET_THOUGHT},
            ),
            (
                "response.output_text.delta",
                {"type": "response.output_text.delta", "delta": "译文。"},
            ),
            (
                "response.completed",
                {"type": "response.completed", "response": {"model": "m1", "status": "completed"}},
            ),
        ]
    )


def _gemini() -> bytes:
    parts = [[{"text": SECRET_THOUGHT, "thought": True}], [{"text": "译文。"}]]
    chunks: list[dict[str, Any]] = [
        {"candidates": [{"content": {"role": "model", "parts": p}}]} for p in parts
    ]
    chunks[-1]["candidates"][0]["finishReason"] = "STOP"
    return _sse([(None, c) for c in chunks])


def _recorded_chat() -> bytes:
    return (FIXTURES / "openai_chat" / "stream_reasoning.sse").read_bytes()


CASES = {
    "anthropic": (AnthropicMessagesLLM, "/v1/messages", _anthropic),
    "chat": (
        OpenAIChatLLM,
        "/chat/completions",
        _recorded_chat,
    ),
    "responses": (OpenAIResponsesLLM, "/responses", _responses),
    "gemini": (GeminiLLM, "/models/m1:streamGenerateContent", _gemini),
}


def _make(cls: type[HttpLLM], router: Router) -> HttpLLM:
    options = {"prices": {"m1": [0, 0]}, "base_url": "https://llm.test/v1"}
    return cls("main", options, "sk-test", transport=router.transport)


@pytest.mark.parametrize("name", CASES)
def test_thinking_is_announced_once_before_text_and_never_leaks(name: str) -> None:
    cls, path, body = CASES[name]
    router = Router().reply(path, body=body(), headers=SSE)
    events = list(_make(cls, router).stream(REQUEST))

    thinking = [i for i, e in enumerate(events) if isinstance(e, Thinking)]
    first_text = next(i for i, e in enumerate(events) if isinstance(e, TextDelta))
    assert len(thinking) == 1 and thinking[0] < first_text
    assert isinstance(events[-1], StreamDone) and events[-1].error is None
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    assert SECRET_THOUGHT not in text
    if name == "chat":  # 真实录制：思考内容很长，正文按编号逐行给出
        assert text.splitlines()[0].startswith("[[1]]")
        assert len(text.splitlines()) == 2
    else:
        assert text == "译文。"
