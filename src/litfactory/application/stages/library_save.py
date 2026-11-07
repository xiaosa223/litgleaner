"""文献库阶段：把用户选中“加入文献库”的论文写进用户的文献管理软件。"""

from collections.abc import Iterable, Mapping
from typing import Any

from litfactory.application.context import AppContext
from litfactory.application.library import library_job_key, save_paper
from litfactory.application.stages.base import Stage
from litfactory.ports.store import JobSpec


class LibrarySaveStage(Stage):
    name = "library"
    reopen_when_planned = True

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        if ctx.library is None:
            return
        for paper_id in ctx.store.feedback.pending_saves(ctx.library.name):
            yield JobSpec(
                key=library_job_key(ctx.library.name, paper_id),
                stage=self.name,
                payload={"paper_id": paper_id},
            )

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        save_paper(ctx, str(payload["paper_id"]))
