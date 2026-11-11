"""综述骨架的显式确认、只读历史与固定文件名下载。"""

from fastapi import FastAPI, Query, Response
from pydantic import Field

from litgleaner.application import outline
from litgleaner.application.survey_plan import require_topic
from litgleaner.domain.outline import TopicOutline
from litgleaner.domain.outline_render import render_outline
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import _Body


class GenerateOutline(_Body):
    plan_hash: str = Field(min_length=1, max_length=200)


class PreviewOutline(_Body):
    pass


_EMPTY = PreviewOutline()


def install_outline_routes(app: FastAPI) -> None:
    @app.post("/api/topics/{topic_id}/outline/preview")
    def preview(
        ctx: Context, topic_id: str, body: PreviewOutline = _EMPTY
    ) -> outline.OutlinePreview:
        return outline.preview(ctx, topic_id)

    @app.post("/api/topics/{topic_id}/outline", status_code=202)
    def generate(ctx: Context, topic_id: str, body: GenerateOutline) -> outline.OutlineStatus:
        return outline.queue(ctx, topic_id, body.plan_hash)

    @app.get("/api/topics/{topic_id}/outline")
    def read(ctx: Context, topic_id: str) -> outline.OutlineView:
        return outline.view(ctx, topic_id)

    @app.get("/api/topics/{topic_id}/outline/jobs/{job_key}")
    def status(ctx: Context, topic_id: str, job_key: str) -> outline.OutlineStatus:
        return outline.status(ctx, topic_id, job_key)

    @app.get("/api/topics/{topic_id}/outline/history")
    def history(
        ctx: Context, topic_id: str, limit: int = Query(default=20, ge=1, le=100)
    ) -> tuple[TopicOutline, ...]:
        require_topic(ctx, topic_id)
        return ctx.store.topic_outlines.history(topic_id, limit)

    @app.get("/api/topics/{topic_id}/outline/results/{outline_id}")
    def result(ctx: Context, topic_id: str, outline_id: str) -> TopicOutline:
        return outline.result(ctx, topic_id, outline_id)

    _install_download(app)


def _install_download(app: FastAPI) -> None:
    @app.get("/api/topics/{topic_id}/exports/outline")
    def download(ctx: Context, topic_id: str, result_id: str = Query(min_length=1)) -> Response:
        saved = outline.result(ctx, topic_id, result_id)
        return Response(
            render_outline(saved),
            media_type="text/markdown",
            headers={
                "Content-Disposition": 'attachment; filename="litgleaner-outline.md"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
