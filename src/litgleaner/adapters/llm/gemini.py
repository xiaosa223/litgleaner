"""Gemini generateContent（POST /v1beta/models/{model}:generateContent）。"""

from typing import Any
from urllib.parse import quote

from litgleaner.adapters.llm.base import HttpLLM, Reply, StreamState, refused, truncated
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

    def _stream_path(self, model: str) -> str:
        return f"/models/{quote(model, safe='')}:streamGenerateContent"

    def _stream_params(self) -> dict[str, str]:
        return {"alt": "sse"}

    def _stream_body(self, request: LLMRequest) -> dict[str, Any]:
        return {
            "systemInstruction": {"parts": [{"text": request.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user}]}],
            "generationConfig": {},
        }

    def _stream_event(self, event: str | None, data: Any, state: StreamState) -> str | None:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            state.stop, state.detail = "refused", f"blockReason={blocked}"
            return None
        state.model = data.get("modelVersion") or state.model
        if usage := data.get("usageMetadata"):
            state.input_tokens = usage.get("promptTokenCount", state.input_tokens)
            state.output_tokens = usage.get("candidatesTokenCount", 0) + usage.get(
                "thoughtsTokenCount", 0
            )
        candidates = data.get("candidates") or []
        if not candidates:
            return None
        finish = candidates[0].get("finishReason")
        if finish in _BLOCKED:
            state.stop, state.detail = "refused", f"finishReason={finish}"
        elif finish == "MAX_TOKENS":
            state.stop = "truncated"
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        return text or None

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
