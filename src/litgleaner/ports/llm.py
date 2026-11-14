"""大模型接口。

- complete：结构化输出，结构由调用方提供，并在网关统一校验。
- list_models：列出提供方的可用模型，不计费（ADR 0013）；不支持的实现不提供这个方法。
- stream：纯文本流式输出（ADR 0004），逐段给出文本增量，最后给出用量；结束后由调用方校验内容。
  推理模型先思考时，在第一段正文之前给出一次 Thinking（不带思考内容），供界面显示状态。
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


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
    cost_usd: float | None  # None：模型价格未知（ADR 0013），不当作 0


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
    cost_usd: float | None  # None：模型价格未知
    estimated: bool = False  # 接口没有返回用量时按文本长度估算
    error: Exception | None = None


StreamEvent = TextDelta | Thinking | StreamDone


@dataclass(frozen=True)
class LLMCall:
    """账本里的一次模型调用。"""

    task: str
    model: str
    prompt_version: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None：价格未知（ADR 0013）
    ok: bool
    paper_id: str | None = None
    scope_id: str | None = None


@dataclass(frozen=True)
class ModelInfo:
    """提供方列出的一个模型。价格单位美元 / 百万 token，读不到时为 None（价格未知）。"""

    id: str
    input_price: float | None = None
    output_price: float | None = None


@runtime_checkable
class ModelCatalog(Protocol):
    def list_models(self) -> tuple[ModelInfo, ...]: ...


class LLM(Protocol):
    def complete(self, request: LLMRequest) -> LLMResponse: ...
    def stream(self, request: TextRequest) -> Iterator[StreamEvent]: ...
    def close(self) -> None: ...

    def price(self, model: str) -> tuple[float, float] | None:
        """美元 / 百万 token（输入, 输出），用于执行前估算费用；不知道时返回 None。"""
        ...
