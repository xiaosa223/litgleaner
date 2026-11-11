"""课题地图的显式预览、生成与只读历史。"""

from fastapi import FastAPI, Query
from pydantic import Field

from litgleaner.application import topic_map
from litgleaner.application.survey_plan import require_topic
from litgleaner.domain.errors import NotFound
from litgleaner.domain.topic_map import TopicMap
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import _Body


class GenerateMap(_Body):
    plan_hash: str = Field(min_length=1, max_length=200)


class PreviewMap(_Body):
    pass


_EMPTY = PreviewMap()


def install_topic_map_routes(app: FastAPI) -> None:
    @app.post("/api/topics/{topic_id}/map/preview")
    def preview(ctx: Context, topic_id: str, body: PreviewMap = _EMPTY) -> topic_map.MapPreview:
        return topic_map.preview(ctx, topic_id)

    @app.post("/api/topics/{topic_id}/map")
    def generate(ctx: Context, topic_id: str, body: GenerateMap) -> topic_map.MapStatus:
        return topic_map.queue(ctx, topic_id, body.plan_hash)

    @app.get("/api/topics/{topic_id}/map")
    def read(ctx: Context, topic_id: str) -> topic_map.MapView:
        return topic_map.view(ctx, topic_id)

    @app.get("/api/topics/{topic_id}/map/jobs/{job_key}")
    def status(ctx: Context, topic_id: str, job_key: str) -> topic_map.MapStatus:
        return topic_map.status(ctx, topic_id, job_key)

    @app.get("/api/topics/{topic_id}/map/history")
    def history(
        ctx: Context, topic_id: str, limit: int = Query(default=20, ge=1, le=100)
    ) -> tuple[TopicMap, ...]:
        require_topic(ctx, topic_id)
        return ctx.store.topic_maps.history(topic_id, limit)

    @app.get("/api/topics/{topic_id}/map/results/{map_id}")
    def result(ctx: Context, topic_id: str, map_id: str) -> TopicMap:
        saved = ctx.store.topic_maps.get(map_id)
        if saved is None or saved.topic_id != topic_id:
            raise NotFound("课题地图不存在")
        return saved
