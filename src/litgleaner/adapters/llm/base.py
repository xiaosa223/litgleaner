"""HTTP 大模型适配器的共同部分。各协议只负责拼请求和读响应。

结构化输出的分工：发给模型的是放宽后的结构（去掉各家不支持的长度、范围、数量约束），
完整结构由网关在本地校验，不合格时按临时错误有限重试。
"""

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, ClassVar, Literal

import httpx
from pydantic import Field

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.options import parse_options
from litgleaner.adapters.llm.sse import iter_sse
from litgleaner.domain.base import FrozenModel
from litgleaner.domain.chunker import estimate_tokens
from litgleaner.domain.errors import (
    ConfigError,
    LitGleanerError,
    ModelUsage,
    PermanentError,
    SchemaChanged,
    TransientError,
)
from litgleaner.ports.llm import (
    LLMRequest,
    LLMResponse,
    StreamDone,
    StreamEvent,
    TextDelta,
    TextRequest,
    Thinking,
)

_UNSUPPORTED = frozenset(
    {
        "minLength",
        "maxLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
    }
)
_NAMED_SCHEMAS = frozenset({"properties", "$defs", "definitions"})
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
_PER_MILLION = 1_000_000
Price = Annotated[float, Field(ge=0)]


class LLMOptions(FrozenModel):
    base_url: str | None = None
    # 美元 / 百万 token（输入, 输出）。每个用到的模型都必须有价格，否则每日预算形同虚设。
    prices: Mapping[str, tuple[Price, Price]] = Field(default_factory=dict)
    max_output_tokens: int = Field(default=4000, gt=0)
    # 请求体里输出上限的字段名，默认按协议。部分兼容接口只认 max_tokens。
    max_tokens_field: str | None = None
    # native：用接口自带的 JSON Schema 约束；prompt：把结构写进系统提示词，适合不支持的接口。
    structured: Literal["native", "prompt"] = "native"
    # 深度合并进请求体，用于各家专有参数，例如 effort、思考预算。
    extra_body: Mapping[str, Any] = Field(default_factory=dict)
    # 不含密钥的附加请求头，例如 beta 开关。
    headers: Mapping[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=180, gt=0)
    min_interval_seconds: float = Field(default=0, ge=0)


@dataclass(frozen=True)
class Reply:
    text: str
    model: str | None
    input_tokens: int
    output_tokens: int


@dataclass
class StreamState:
    """一次流式调用内累积的信息。只在这次调用里修改。"""

    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    stop: Literal["truncated", "refused"] | None = None
    detail: str = ""
    thinking: bool = False  # 适配器看到思考内容时置为 True
    text: list[str] = field(default_factory=list)


def relax_schema(node: Any) -> Any:
    """返回去掉不受支持约束的新结构。properties 下的键是字段名，不当作关键字处理。"""
    if isinstance(node, list | tuple):
        return [relax_schema(item) for item in node]
    if not isinstance(node, Mapping):
        return node
    relaxed: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED:
            continue
        if key in _NAMED_SCHEMAS and isinstance(value, Mapping):
            relaxed[key] = {name: relax_schema(sub) for name, sub in value.items()}
        else:
            relaxed[key] = relax_schema(value)
    return relaxed


def deep_merge(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def refused(service: str, detail: str) -> PermanentError:
    return PermanentError(f"{service} 拒绝回答：{detail[:200]}")


def truncated(service: str) -> TransientError:
    return TransientError(f"{service} 输出被截断，可调大 max_output_tokens 或降低思考强度")


def token_count(value: Any) -> int:
    """用量必须是接口明确报告的非负整数，不能把缺失或异常值当作免费。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("响应没有有效的 token 用量")
    return value


def _embedded_object(text: str) -> dict[str, Any] | None:
    """部分模型在 JSON 前后夹带标记（如 <escape>）或一句话：只接受唯一一个完整对象。"""
    start = text.find("{")
    if start < 0 or "[" in text[:start]:
        return None
    try:
        data, end = json.JSONDecoder().raw_decode(text, start)
    except json.JSONDecodeError:
        return None
    rest = text[end:]
    if not isinstance(data, dict) or "{" in rest or "[" in rest:
        return None
    return data


def parse_json_object(text: str, service: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as error:
        data = _embedded_object(stripped)
        if data is None:
            raise TransientError(f"{service} 返回的不是 JSON：{stripped[:80]!r}") from error
    if not isinstance(data, dict):
        raise TransientError(f"{service} 返回的 JSON 不是对象")
    return data


def schema_instruction(schema: Mapping[str, Any]) -> str:
    text = json.dumps(schema, ensure_ascii=False)
    return f"\n\n只输出一个 JSON 对象，不要输出其他文字。它必须符合这个 JSON Schema：\n{text}"


class HttpLLM:
    """子类填写协议细节：默认地址、鉴权头、路径、请求体、响应解析。"""

    protocol: ClassVar[str]
    default_base_url: ClassVar[str]
    default_max_tokens_field: ClassVar[str]

    def __init__(
        self,
        name: str,
        options: Mapping[str, Any],
        api_key: str | None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.service = f"{name}（{self.protocol}）"
        self._options = parse_options(LLMOptions, options, self.service)
        self._http = HttpClient(
            self.service,
            base_url=self._options.base_url or self.default_base_url,
            secrets=(api_key,),
            headers={**self._options.headers, **(self._auth_headers(api_key) if api_key else {})},
            min_interval=self._options.min_interval_seconds,
            timeout=self._options.timeout_seconds,
            transport=transport,
            keep_raw=False,  # 请求里是论文内容，不在内存里留档
        )

    def price(self, model: str) -> tuple[float, float] | None:
        found = self._options.prices.get(model)
        return None if found is None else (float(found[0]), float(found[1]))

    def check_model(self, model: str) -> None:
        if model not in self._options.prices:
            raise ConfigError(f"{self.service} 的 options.prices 缺少模型 {model} 的价格")

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.check_model(request.model)
        native = self._options.structured == "native"
        system = request.system if native else request.system + schema_instruction(request.schema)
        body = self._body(request, system, relax_schema(request.schema) if native else None)
        self._set_output_limit(
            body,
            self._options.max_tokens_field or self.default_max_tokens_field,
            self._options.max_output_tokens,
        )
        data = self._http.post_json(
            self._path(request.model), deep_merge(body, self._options.extra_body)
        )
        try:
            return self._response(data, request.model)
        except LitGleanerError as error:
            error.model_usage = self._known_usage(data, request.model)
            raise

    def _response(self, data: Any, model: str) -> LLMResponse:
        try:
            reply = self._reply(data)
        except (KeyError, IndexError, TypeError, AttributeError) as error:
            raise SchemaChanged(
                f"{self.service} 返回的结构与预期不符：{type(error).__name__} {error}"
            ) from error
        price_in, price_out = self._options.prices[model]
        cost = (reply.input_tokens * price_in + reply.output_tokens * price_out) / _PER_MILLION
        return LLMResponse(
            data=parse_json_object(reply.text, self.service),
            model=reply.model or model,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            cost_usd=cost,
        )

    def _known_usage(self, data: Any, model: str) -> ModelUsage | None:
        try:
            reported_model, tokens_in, tokens_out = self._usage(data)
            tokens_in, tokens_out = token_count(tokens_in), token_count(tokens_out)
        except (KeyError, IndexError, TypeError, AttributeError, ValueError):
            return None
        price_in, price_out = self._options.prices[model]
        return ModelUsage(
            reported_model if isinstance(reported_model, str) and reported_model else model,
            tokens_in,
            tokens_out,
            (tokens_in * price_in + tokens_out * price_out) / _PER_MILLION,
        )

    def stream(self, request: TextRequest) -> Iterator[StreamEvent]:
        """纯文本流式输出。

        最后一个事件是 StreamDone；出错时先给出 StreamDone（带已知用量）再抛出。
        """
        self.check_model(request.model)
        as_request = LLMRequest(request.task, request.model, request.system, request.user, {})
        body = self._stream_body(as_request)
        field_name = self._options.max_tokens_field or self.default_max_tokens_field
        self._set_output_limit(body, field_name, self._options.max_output_tokens)
        state = StreamState()
        lines = self._http.post_lines(
            self._stream_path(request.model),
            deep_merge(body, self._options.extra_body),
            params=self._stream_params(),
        )
        announced = False
        try:
            for event, raw in iter_sse(lines):
                delta = self._parse_event(event, raw, state)
                if state.thinking and not announced and not state.text:
                    announced = True
                    yield Thinking()
                if delta:
                    state.text.append(delta)
                    yield TextDelta(delta)
            self._check_stop(state)
        except LitGleanerError as error:
            yield self._done(request, state, error)
            raise
        yield self._done(request, state, None)

    def _parse_event(self, event: str | None, raw: str, state: StreamState) -> str | None:
        if raw.strip() == "[DONE]":
            return None
        try:
            return self._stream_event(event, json.loads(raw), state)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
            raise SchemaChanged(
                f"{self.service} 流式事件结构与预期不符：{type(error).__name__} {error}"
            ) from error

    def _check_stop(self, state: StreamState) -> None:
        if state.stop == "refused":
            raise refused(self.service, state.detail or "流式输出被拒绝")
        if state.stop == "truncated":
            raise truncated(self.service)

    def _done(
        self, request: TextRequest, state: StreamState, error: LitGleanerError | None
    ) -> StreamDone:
        """成功但接口没给用量时按文本长度估算，不能当作免费；失败时只记已知用量。"""
        tokens_in, tokens_out = state.input_tokens, state.output_tokens
        estimated = error is None and (tokens_in is None or tokens_out is None)
        if estimated:
            tokens_in = tokens_in or estimate_tokens(request.system + request.user)
            tokens_out = tokens_out or estimate_tokens("".join(state.text))
        price_in, price_out = self._options.prices[request.model]
        tokens_in, tokens_out = tokens_in or 0, tokens_out or 0
        return StreamDone(
            model=state.model or request.model,
            input_tokens=tokens_in,
            output_tokens=tokens_out,
            cost_usd=(tokens_in * price_in + tokens_out * price_out) / _PER_MILLION,
            estimated=estimated,
            error=error,
        )

    def close(self) -> None:
        self._http.close()

    def _stream_body(self, request: LLMRequest) -> dict[str, Any]:
        body = self._body(request, request.system, None)
        body["stream"] = True
        return body

    def _stream_path(self, model: str) -> str:
        return self._path(model)

    def _stream_params(self) -> dict[str, str] | None:
        return None

    def _stream_event(self, event: str | None, data: Any, state: StreamState) -> str | None:
        raise NotImplementedError

    def _set_output_limit(self, body: dict[str, Any], field: str, value: int) -> None:
        body[field] = value

    def _auth_headers(self, api_key: str) -> dict[str, str]:
        raise NotImplementedError

    def _path(self, model: str) -> str:
        raise NotImplementedError

    def _body(
        self, request: LLMRequest, system: str, schema: dict[str, Any] | None
    ) -> dict[str, Any]:
        raise NotImplementedError

    def _reply(self, data: Any) -> Reply:
        raise NotImplementedError

    def _usage(self, data: Any) -> tuple[str | None, int, int]:
        """独立于输出内容提取已知用量；缺失时抛解析异常，由公共层保留未知状态。"""
        raise NotImplementedError
