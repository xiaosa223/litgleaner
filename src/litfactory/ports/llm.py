"""大模型接口。输出结构由调用方提供，并在网关统一校验。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class LLMRequest:
    task: str
    model: str
    system: str
    user: str
    schema: Mapping[str, Any]


@dataclass(frozen=True)
class LLMResponse:
    data: Mapping[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class LLM(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...
