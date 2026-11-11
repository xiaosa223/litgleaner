"""假大模型：输出完全确定。论文内容里的标记决定结果，方便测试和演示。"""

import re
from collections.abc import Iterator, Mapping
from typing import Any

from litgleaner.ports.llm import (
    LLMRequest,
    LLMResponse,
    StreamDone,
    StreamEvent,
    TextDelta,
    TextRequest,
)

_LEVEL = re.compile(r"\[\[level:(\d)\]\]")
_DEFAULT_LEVEL = 3
_COST_PER_CALL = 0.001
_UNTRUSTED = re.compile(r"</?untrusted_paper>")
_MARKER = re.compile(r"^\s*(\[\[\d+\]\])\s*(.*)$")


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []
        self.fail_next: list[Exception] = []
        self.fail_tasks: dict[str, list[Exception]] = {}
        self.overrides: dict[str, Mapping[str, Any]] = {}
        self.text_calls: list[TextRequest] = []
        self.text_overrides: dict[str, str] = {}

    def price(self, model: str) -> tuple[float, float] | None:
        """固定价格，便于测试费用估算。"""
        return (1.0, 2.0)

    def close(self) -> None:
        """假适配器没有需要关闭的资源。"""

    def stream(self, request: TextRequest) -> Iterator[StreamEvent]:
        """纯文本流：默认把每个编号行译成“[[n]] 【译】原文”，分两段吐出，便于测试增量显示。"""
        self.text_calls.append(request)
        if self.fail_next:
            raise self.fail_next.pop(0)
        text = self.text_overrides.get(request.task)
        if text is None:
            inner = _UNTRUSTED.sub("", request.user)
            matches = (_MARKER.match(line) for line in inner.splitlines())
            text = "\n".join(f"{m.group(1)} 【译】{m.group(2)}" for m in matches if m)
        middle = len(text) // 2
        for piece in (text[:middle], text[middle:]):
            if piece:
                yield TextDelta(piece)
        yield StreamDone(request.model, 100, 20, _COST_PER_CALL)

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
