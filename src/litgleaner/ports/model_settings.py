"""模型原地配置：状态、临时客户端和持久化运行时替换。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from litgleaner.ports.llm import LLM

ModelTask = Literal["triage", "extract", "assess", "translate", "ask", "map", "outline"]


@dataclass(frozen=True)
class ModelDraft:
    provider: str
    protocol: str
    model: str
    tasks: tuple[ModelTask, ...]
    input_price: float
    output_price: float
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    daily_budget_usd: float = 2.0
    max_output_tokens: int = 4000
    structured: Literal["native", "prompt"] = "native"
    task_budgets_usd: Mapping[ModelTask, float | None] = field(default_factory=dict)


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

    def save(self, draft: ModelDraft) -> ModelSettingsView:
        """原子保存并替换后续请求使用的运行时；不调用模型。"""
        ...
