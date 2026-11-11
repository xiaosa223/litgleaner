"""大模型接口。

- complete：结构化输出，结构由调用方提供，并在网关统一校验。
- stream：纯文本流式输出（ADR 0004），逐段给出文本增量，最后给出用量；结束后由调用方校验内容。
  推理模型先思考时，在第一段正文之前给出一次 Thinking（不带思考内容），供界面显示状态。
"""

from collections.abc import Iterator, Mapping
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


@dataclass(frozen=True)
class TextRequest:
    task: str
    model: str
    system: str
    user: str


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Thinking:
    """模型正在思考，还没有正文。每次调用最多出现一次，思考内容不外传。"""


@dataclass(frozen=True)
class StreamDone:
    """流的最后一个事件。出错时同样给出，带着已知的用量，随后抛出 error。"""

    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    estimated: bool = False  # 接口没有返回用量时按文本长度估算
    error: Exception | None = None


StreamEvent = TextDelta | Thinking | StreamDone


class LLM(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...
    def stream(self, request: TextRequest) -> Iterator[StreamEvent]: ...
    def close(self) -> None: ...

    def price(self, model: str) -> tuple[float, float] | None:
        """美元 / 百万 token（输入, 输出），用于执行前估算费用；不知道时返回 None。"""
        ...
