"""阶段约定：plan 找出待办，process 处理一项。两者都不感知调度和重试。"""

from collections.abc import Iterable, Mapping
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.ports.store import JobSpec


class Stage:
    name: str = ""
    # plan 只在输出和期望不一致时才给出任务，所以已完成的同名任务也要重新执行
    reopen_when_planned: bool = False

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        raise NotImplementedError

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        raise NotImplementedError

    def on_dead(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        """任务彻底失败后的善后。默认不需要。"""
