"""HTTP 大模型适配器的共同部分。各协议只负责拼请求和读响应。

结构化输出的分工：发给模型的是放宽后的结构（去掉各家不支持的长度、范围、数量约束），
完整结构由网关在本地校验，不合格时按临时错误有限重试。
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, ClassVar, Literal

import httpx
from pydantic import Field

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.options import parse_options
from litgleaner.domain.base import FrozenModel
from litgleaner.domain.errors import ConfigError, PermanentError, SchemaChanged, TransientError
from litgleaner.ports.llm import LLMRequest, LLMResponse

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


def parse_json_object(text: str, service: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as error:
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
            reply = self._reply(data)
        except (KeyError, IndexError, TypeError, AttributeError) as error:
            raise SchemaChanged(
                f"{self.service} 返回的结构与预期不符：{type(error).__name__} {error}"
            ) from error
        price_in, price_out = self._options.prices[request.model]
        cost = (reply.input_tokens * price_in + reply.output_tokens * price_out) / _PER_MILLION
        return LLMResponse(
            data=parse_json_object(reply.text, self.service),
            model=reply.model or request.model,
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            cost_usd=cost,
        )

    def close(self) -> None:
        self._http.close()

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
