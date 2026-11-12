"""关系预览、显式核实及反馈，沿用本地会话和CSRF保护。"""

from fastapi import FastAPI
from pydantic import Field

from litgleaner.application import relate, relate_backfill, relation_feedback, relation_views
from litgleaner.domain.paper_relation import (
    PaperRelation,
    RelateBatch,
    RelationAction,
    RelationKind,
)
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import _Body


class ConfirmRelate(_Body):
    plan_hash: str = Field(min_length=1, max_length=200)


class RelationFeedback(_Body):
    request_id: str = Field(min_length=1, max_length=200)
    action: RelationAction
    kind: RelationKind | None = None


def install_relation_routes(app: FastAPI) -> None:
    @app.post("/api/papers/{paper_id}/relations/preview")
    def preview(ctx: Context, paper_id: str) -> relate.RelatePreview:
        return relate.preview(ctx, paper_id)

    @app.post("/api/papers/{paper_id}/relations", status_code=202)
    def generate(ctx: Context, paper_id: str, body: ConfirmRelate) -> relate.RelateStatus:
        return relate.queue(ctx, paper_id, body.plan_hash)

    @app.get("/api/papers/{paper_id}/relations")
    def read(ctx: Context, paper_id: str) -> relation_views.RelationView:
        return relation_views.view(ctx, paper_id)

    @app.get("/api/papers/{paper_id}/relations/jobs/{job_key}")
    def status(ctx: Context, paper_id: str, job_key: str) -> relate.RelateStatus:
        return relate.status(ctx, paper_id, job_key)

    @app.get("/api/papers/{paper_id}/relations/history")
    def history(ctx: Context, paper_id: str) -> tuple[RelateBatch, ...]:
        return relation_views.view(ctx, paper_id).batches

    @app.get("/api/papers/{paper_id}/relations/results/{batch_id}")
    def result(ctx: Context, paper_id: str, batch_id: str) -> RelateBatch:
        return relation_views.batch(ctx, paper_id, batch_id)

    @app.post("/api/relations/{relation_id}/feedback")
    def feedback(ctx: Context, relation_id: str, body: RelationFeedback) -> PaperRelation:
        return relation_feedback.apply(
            ctx, relation_id, body.action, body.request_id, kind=body.kind
        )

    _backfill_routes(app)


def _backfill_routes(app: FastAPI) -> None:
    @app.post("/api/memory/backfill/preview")
    def preview(ctx: Context) -> relate_backfill.BackfillPreview:
        return relate_backfill.preview(ctx)

    @app.post("/api/memory/backfill", status_code=202)
    def execute(ctx: Context, body: ConfirmRelate) -> tuple[relate.RelateStatus, ...]:
        return relate_backfill.queue(ctx, body.plan_hash)
