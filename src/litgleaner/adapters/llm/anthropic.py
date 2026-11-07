"""Anthropic Messages API（POST /v1/messages）。中转站的 base_url 写到域名为止，不带 /v1。"""

from typing import Any

from litgleaner.adapters.llm.base import HttpLLM, Reply, StreamState, refused, truncated
from litgleaner.domain.errors import TransientError
from litgleaner.ports.llm import LLMRequest

API_VERSION = "2023-06-01"


class AnthropicMessagesLLM(HttpLLM):
    protocol = "anthropic_messages"
    default_base_url = "https://api.anthropic.com"
    default_max_tokens_field = "max_tokens"

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        return {"x-api-key": api_key, "anthropic-version": API_VERSION}

    def _path(self, model: str) -> str:
        return "/v1/messages"

    def _body(
        self, request: LLMRequest, system: str, schema: dict[str, Any] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "system": system,
            "messages": [{"role": "user", "content": request.user}],
        }
        if schema is not None:
            body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
        return body

    def _stream_event(self, event: str | None, data: Any, state: StreamState) -> str | None:
        kind = data.get("type", event)
        if kind == "message_start":
            message = data["message"]
            state.model = message.get("model")
            state.input_tokens = _input_tokens(message.get("usage") or {})
        elif kind == "content_block_delta" and data["delta"].get("type") == "text_delta":
            return str(data["delta"]["text"])
        elif kind == "message_delta":
            state.output_tokens = (data.get("usage") or {}).get(
                "output_tokens", state.output_tokens
            )
            stop = data["delta"].get("stop_reason")
            if stop == "max_tokens":
                state.stop = "truncated"
            elif stop == "refusal":
                state.stop, state.detail = "refused", "stop_reason=refusal"
        elif kind == "error":
            message = (data.get("error") or {}).get("message", "")
            raise TransientError(f"{self.service} 流式输出出错：{message[:200]}")
        return None

    def _reply(self, data: Any) -> Reply:
        stop = data.get("stop_reason")
        text = "".join(b["text"] for b in data["content"] if b.get("type") == "text")
        if stop == "refusal":
            raise refused(self.service, text or "stop_reason=refusal")
        if stop == "max_tokens":
            raise truncated(self.service)
        usage = data["usage"]
        return Reply(text, data.get("model"), _input_tokens(usage), usage["output_tokens"])


def _input_tokens(usage: dict[str, Any]) -> int:
    return int(
        usage["input_tokens"]
        + (usage.get("cache_creation_input_tokens") or 0)
        + (usage.get("cache_read_input_tokens") or 0)
    )
