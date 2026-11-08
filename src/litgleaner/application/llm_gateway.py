"""所有 LLM 调用的唯一入口：预算、提示词、不可信隔离、输出校验、费用记录。"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator

from litgleaner.domain.errors import BudgetExceeded, TransientError
from litgleaner.domain.evidence import wrap_untrusted
from litgleaner.domain.hashing import stable_hash
from litgleaner.ports.llm import (
    LLM,
    LLMRequest,
    LLMResponse,
    StreamDone,
    TextDelta,
    TextRequest,
    Thinking,
)
from litgleaner.ports.store import LLMCall, RunRepo


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


@dataclass(frozen=True)
class TextResult:
    text: str
    model: str
    prompt_version: str


class TextStream:
    """流式文本：迭代得到文本增量，推理模型思考时先得到一次 Thinking；迭代完成后 result 才有值。

    费用在流结束时（包括出错时）记录。
    """

    def __init__(
        self, gateway: "LLMGateway", task: str, request: TextRequest, paper_id: str | None
    ) -> None:
        self._gateway = gateway
        self._task = task
        self._request = request
        self._paper_id = paper_id
        self.result: TextResult | None = None

    def __iter__(self) -> Iterator[str | Thinking]:
        route = self._gateway.routes[self._task]
        parts: list[str] = []
        for event in route.llm.stream(self._request):
            if isinstance(event, Thinking):
                yield event
            elif isinstance(event, TextDelta):
                parts.append(event.text)
                yield event.text
            elif isinstance(event, StreamDone):
                self._gateway.record_stream(self._task, event, self._paper_id)
                if event.error is None:
                    self.result = TextResult("".join(parts), event.model, route.prompt.version)


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

    def stream(
        self,
        task: str,
        variables: Mapping[str, str],
        untrusted: str,
        *,
        paper_id: str | None = None,
    ) -> TextStream:
        """纯文本流式调用（ADR 0004）。预算在开始前检查；内容由调用方在结束后校验。"""
        self._check_budget()
        route = self.routes[task]
        request = TextRequest(
            task=task,
            model=route.model,
            system=route.prompt.render(variables),
            user=wrap_untrusted(untrusted),
        )
        return TextStream(self, task, request, paper_id)

    def record_stream(self, task: str, done: StreamDone, paper_id: str | None) -> None:
        self.runs.record_llm_call(
            LLMCall(
                task=task,
                model=done.model,
                prompt_version=self.routes[task].prompt.version,
                input_tokens=done.input_tokens,
                output_tokens=done.output_tokens,
                cost_usd=done.cost_usd,
                ok=done.error is None,
                paper_id=paper_id,
            ),
            self.clock().isoformat(),
        )

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
