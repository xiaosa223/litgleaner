"""用户显式确认的历史重提取；失败不改变全文状态。"""

from collections.abc import Iterable, Mapping
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.reextract_run import process
from litgleaner.application.stages.base import Stage
from litgleaner.ports.store import JobSpec


class ReextractStage(Stage):
    name = "reextract"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        process(ctx, payload)
