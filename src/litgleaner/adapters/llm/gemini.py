"""Gemini generateContent（POST /v1beta/models/{model}:generateContent）。"""

from typing import Any
from urllib.parse import quote

from litgleaner.adapters.llm.base import HttpLLM, Reply, refused, truncated
from litgleaner.ports.llm import LLMRequest

_BLOCKED = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
)


class GeminiLLM(HttpLLM):
    protocol = "gemini"
    default_base_url = "https://generativelanguage.googleapis.com/v1beta"
    default_max_tokens_field = "maxOutputTokens"

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        return {"x-goog-api-key": api_key}

    def _path(self, model: str) -> str:
        return f"/models/{quote(model, safe='')}:generateContent"

    def _body(
        self, request: LLMRequest, system: str, schema: dict[str, Any] | None
    ) -> dict[str, Any]:
        config: dict[str, Any] = {"responseMimeType": "application/json"}
        if schema is not None:
            config["responseJsonSchema"] = schema
        return {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": config,
        }

    def _set_output_limit(self, body: dict[str, Any], field: str, value: int) -> None:
        body["generationConfig"][field] = value

    def _reply(self, data: Any) -> Reply:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise refused(self.service, f"blockReason={blocked}")
        candidate = data["candidates"][0]
        finish = candidate.get("finishReason")
        if finish in _BLOCKED:
            raise refused(self.service, f"finishReason={finish}")
        if finish == "MAX_TOKENS":
            raise truncated(self.service)
        parts = candidate["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        usage = data["usageMetadata"]
        tokens_out = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
        return Reply(text, data.get("modelVersion"), usage["promptTokenCount"], tokens_out)
