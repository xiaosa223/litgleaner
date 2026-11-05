"""假大模型：输出完全确定。论文内容里的标记决定结果，方便测试和演示。"""

import re
from collections.abc import Mapping
from typing import Any

from litfactory.ports.llm import LLMRequest, LLMResponse

_LEVEL = re.compile(r"\[\[level:(\d)\]\]")
_DEFAULT_LEVEL = 3
_COST_PER_CALL = 0.001


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []
        self.fail_next: list[Exception] = []
        self.fail_tasks: dict[str, list[Exception]] = {}
        self.overrides: dict[str, Mapping[str, Any]] = {}

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self.fail_next:
            raise self.fail_next.pop(0)
        if self.fail_tasks.get(request.task):
            raise self.fail_tasks[request.task].pop(0)
        data = self.overrides.get(request.task) or self._answer(request)
        return LLMResponse(data, request.model, 100, 20, _COST_PER_CALL)

    def _answer(self, request: LLMRequest) -> Mapping[str, Any]:
        if request.task == "triage":
            relevant = "[[irrelevant]]" not in request.user
            return {"relevant": relevant, "reason": "演示判断"}
        if request.task == "extract":
            return {
                "summary": "演示总结",
                "conclusion": "演示结论",
                "innovation": "演示创新点",
                "limitations": [
                    {"text": "演示不足", "source": "author", "quote": "We found it works."}
                ],
            }
        return self._assessment(request)

    def _assessment(self, request: LLMRequest) -> Mapping[str, Any]:
        match = _LEVEL.search(request.user)
        level = int(match.group(1)) if match else _DEFAULT_LEVEL
        keys = request.schema["properties"]["dimensions"]["required"]
        answer = {"level": level, "reason": "演示理由", "quote": None}
        return {"dimensions": dict.fromkeys(keys, answer)}
