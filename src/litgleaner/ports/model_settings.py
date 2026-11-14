"""模型原地配置：状态、临时客户端和持久化运行时替换。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from litgleaner.ports.llm import LLM

# 兼容接口常只认 max_tokens；None 表示沿用已有配置或协议默认字段。
MaxTokensField = Literal["max_tokens", "max_completion_tokens"]
# 连接测试的输出上限：推理模型先思考再作答，太小会把可用模型误报为失败（实测约 250 token）。
PROBE_MAX_OUTPUT_TOKENS = 1024
ModelTask = Literal[
    "triage",
    "extract",
    "assess",
    "translate",
    "ask",
    "map",
    "outline",
    "relate",
    "draft",
    "guide",
    "evaluate",
    "profile",
]


@dataclass(frozen=True)
class ModelDraft:
    provider: str
    protocol: str
    model: str
    tasks: tuple[ModelTask, ...]
    input_price: float | None  # None：价格未知（ADR 0013）
    output_price: float | None
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    daily_budget_usd: float = 2.0
    max_output_tokens: int = 4000
    structured: Literal["native", "prompt"] = "native"
    max_tokens_field: MaxTokensField | None = None
    task_budgets_usd: Mapping[ModelTask, float | None] = field(default_factory=dict)


@dataclass(frozen=True)
class CatalogDraft:
    """列模型只需要连接信息；api_key 省略时沿用已保存提供方的密钥。"""

    provider: str
    protocol: str
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None


@dataclass(frozen=True)
class ModelProviderView:
    name: str
    protocol: str
    enabled: bool
    base_url: str | None
    credential_set: bool
    credential_last_four: str | None
    demonstration: bool


@dataclass(frozen=True)
class ModelTaskView:
    task: str
    provider: str | None
    model: str | None
    ready: bool
    demonstration: bool = False
    daily_budget_usd: float | None = None


@dataclass(frozen=True)
class ModelSettingsView:
    providers: tuple[ModelProviderView, ...]
    tasks: tuple[ModelTaskView, ...]
    protocols: tuple[str, ...]
    daily_budget_usd: float


class ModelSettings(Protocol):
    def view(self) -> ModelSettingsView: ...
    def client(self, draft: ModelDraft) -> LLM:
        """临时客户端不写配置或密钥；测试请求限制输出长度。"""
        ...

    def catalog_client(self, source: CatalogDraft) -> LLM:
        """只用于列模型的临时客户端（ADR 0013），不写配置或密钥。"""
        ...

    def save(self, draft: ModelDraft) -> ModelSettingsView:
        """原子保存并替换后续请求使用的运行时；不调用模型。"""
        ...


def probe_output_tokens(draft: ModelDraft) -> int:
    """连接测试实际使用的输出上限；发送和费用估计共用。"""
    return min(draft.max_output_tokens, PROBE_MAX_OUTPUT_TOKENS)
