"""OpenAI 的两种协议。base_url 写到 /v1 为止，国内厂商和中转站的兼容接口也走这里。"""

from typing import Any

from litgleaner.adapters.llm.base import HttpLLM, Reply, refused, truncated
from litgleaner.ports.llm import LLMRequest

DEFAULT_BASE_URL = "https://api.openai.com/v1"


def _bearer(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


class OpenAIChatLLM(HttpLLM):
    """Chat Completions（POST /chat/completions）。"""

    protocol = "openai_chat"
    default_base_url = DEFAULT_BASE_URL
    default_max_tokens_field = "max_completion_tokens"

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        return _bearer(api_key)

    def _path(self, model: str) -> str:
        return "/chat/completions"

    def _body(
        self, request: LLMRequest, system: str, schema: dict[str, Any] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": request.user},
            ],
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": request.task, "schema": schema, "strict": True},
            }
        return body

    def _reply(self, data: Any) -> Reply:
        choice = data["choices"][0]
        message = choice["message"]
        if message.get("refusal"):
            raise refused(self.service, message["refusal"])
        if choice.get("finish_reason") == "length":
            raise truncated(self.service)
        if choice.get("finish_reason") == "content_filter":
            raise refused(self.service, "finish_reason=content_filter")
        usage = data["usage"]
        return Reply(
            message["content"] or "",
            data.get("model"),
            usage["prompt_tokens"],
            usage["completion_tokens"],
        )


class OpenAIResponsesLLM(HttpLLM):
    """Responses API（POST /responses）。store=false：不在 OpenAI 侧保存论文内容。"""

    protocol = "openai_responses"
    default_base_url = DEFAULT_BASE_URL
    default_max_tokens_field = "max_output_tokens"

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        return _bearer(api_key)

    def _path(self, model: str) -> str:
        return "/responses"

    def _body(
        self, request: LLMRequest, system: str, schema: dict[str, Any] | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model,
            "instructions": system,
            "input": request.user,
            "store": False,
        }
        if schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.task,
                    "schema": schema,
                    "strict": True,
                }
            }
        return body

    def _reply(self, data: Any) -> Reply:
        parts = [
            part
            for item in data["output"]
            if item.get("type") == "message"
            for part in item["content"]
        ]
        refusal = next((p["refusal"] for p in parts if p.get("type") == "refusal"), None)
        if refusal is not None:
            raise refused(self.service, refusal)
        if data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason")
            if reason == "content_filter":
                raise refused(self.service, "incomplete: content_filter")
            raise truncated(self.service)
        text = "".join(p["text"] for p in parts if p.get("type") == "output_text")
        usage = data["usage"]
        return Reply(text, data.get("model"), usage["input_tokens"], usage["output_tokens"])
