"""课题的只读阅读路线接口。"""

from fastapi import FastAPI, Query

from litgleaner.application.reading_route import ReadingRoute, reading_route
from litgleaner.server.runtime import Context


def install_topic_routes(app: FastAPI) -> None:
    @app.get("/api/topics/{topic_id}/reading-route")
    def route(
        ctx: Context, topic_id: str, limit_per_group: int = Query(default=10, ge=1, le=100)
    ) -> ReadingRoute:
        return reading_route(ctx, topic_id, limit_per_group=limit_per_group)
