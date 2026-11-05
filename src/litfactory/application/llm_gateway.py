"""所有 LLM 调用的唯一入口：预算、提示词、不可信隔离、输出校验、费用记录。"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator

from litfactory.domain.errors import BudgetExceeded, TransientError
from litfactory.domain.evidence import wrap_untrusted
from litfactory.domain.hashing import stable_hash
from litfactory.ports.llm import LLM, LLMRequest, LLMResponse
from litfactory.ports.store import LLMCall, RunRepo


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str

    @property
    def version(self) -> str:
        return stable_hash(self.text)

    def render(self, variables: Mapping[str, str]) -> str:
        rendered = self.text
        for name, value in variables.items():
            rendered = rendered.replace("{{" + name + "}}", value)
        return rendered


@dataclass(frozen=True)
class TaskRoute:
    llm: LLM
    model: str
    prompt: Prompt


@dataclass(frozen=True)
class LLMResult:
    data: Mapping[str, Any]
    model: str
    prompt_version: str


class LLMOutputInvalid(TransientError):
    """模型输出不符合结构。可能是偶发，有限重试，超过次数进入死信。"""


@dataclass(frozen=True)
class LLMGateway:
    routes: Mapping[str, TaskRoute]
    runs: RunRepo
    daily_budget_usd: float
    clock: Callable[[], datetime]

    def prompt_version(self, task: str) -> str:
        return self.routes[task].prompt.version

    def call(
        self,
        task: str,
        variables: Mapping[str, str],
        untrusted: str,
        schema: Mapping[str, Any],
    ) -> LLMResult:
        self._check_budget()
        route = self.routes[task]
        response = route.llm.complete(
            LLMRequest(
                task=task,
                model=route.model,
                system=route.prompt.render(variables),
                user=wrap_untrusted(untrusted),
                schema=schema,
            )
        )
        errors = sorted(Draft202012Validator(schema).iter_errors(response.data), key=str)
        self._record(task, route, response, ok=not errors)
        if errors:
            raise LLMOutputInvalid(f"{task} 输出不符合结构：{errors[0].message[:200]}")
        return LLMResult(response.data, response.model, route.prompt.version)

    def _check_budget(self) -> None:
        now = self.clock().astimezone(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent = self.runs.llm_cost_since(day_start.isoformat())
        if spent >= self.daily_budget_usd:
            raise BudgetExceeded(f"今日 LLM 费用 {spent:.4f} 已达预算 {self.daily_budget_usd}")

    def _record(self, task: str, route: TaskRoute, response: LLMResponse, ok: bool) -> None:
        self.runs.record_llm_call(
            LLMCall(
                task=task,
                model=response.model,
                prompt_version=route.prompt.version,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cost_usd=response.cost_usd,
                ok=ok,
            ),
            self.clock().isoformat(),
        )
