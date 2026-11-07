"""Anthropic Messages API（POST /v1/messages）。中转站的 base_url 写到域名为止，不带 /v1。"""

from typing import Any

from litgleaner.adapters.llm.base import HttpLLM, Reply, refused, truncated
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

    def _reply(self, data: Any) -> Reply:
        stop = data.get("stop_reason")
        text = "".join(b["text"] for b in data["content"] if b.get("type") == "text")
        if stop == "refusal":
            raise refused(self.service, text or "stop_reason=refusal")
        if stop == "max_tokens":
            raise truncated(self.service)
        usage = data["usage"]
        tokens_in = (
            usage["input_tokens"]
            + (usage.get("cache_creation_input_tokens") or 0)
            + (usage.get("cache_read_input_tokens") or 0)
        )
        return Reply(text, data.get("model"), tokens_in, usage["output_tokens"])
