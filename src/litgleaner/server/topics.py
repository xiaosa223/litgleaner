"""课题的只读阅读路线接口。"""

from fastapi import FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field

from litgleaner.application.reading_route import ReadingRoute, reading_route
from litgleaner.application.survey import SurveyStatus, get_preview, preview, queue, status
from litgleaner.application.survey_plan import require_topic
from litgleaner.domain.survey import SurveyPreview, SurveyRound
from litgleaner.server.runtime import Context


class ConfirmSurvey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=1, max_length=200)


class PreviewSurvey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_EMPTY_PREVIEW = PreviewSurvey()


def install_topic_routes(app: FastAPI) -> None:
    @app.get("/api/topics/{topic_id}/reading-route")
    def route(
        ctx: Context, topic_id: str, limit_per_group: int = Query(default=10, ge=1, le=100)
    ) -> ReadingRoute:
        return reading_route(ctx, topic_id, limit_per_group=limit_per_group)

    @app.post("/api/topics/{topic_id}/survey/preview")
    def survey_preview(
        ctx: Context, topic_id: str, body: PreviewSurvey = _EMPTY_PREVIEW
    ) -> SurveyPreview:
        return preview(ctx, topic_id)

    @app.get("/api/topics/{topic_id}/survey/plans/{plan_id}")
    def survey_plan(ctx: Context, topic_id: str, plan_id: str) -> SurveyPreview:
        return get_preview(ctx, topic_id, plan_id)

    @app.post("/api/topics/{topic_id}/survey")
    def survey_confirm(ctx: Context, topic_id: str, body: ConfirmSurvey) -> SurveyStatus:
        return queue(ctx, topic_id, body.plan_id)

    @app.get("/api/topics/{topic_id}/survey/runs/{run_id}")
    def survey_status(ctx: Context, topic_id: str, run_id: str) -> SurveyStatus:
        return status(ctx, topic_id, run_id)

    @app.get("/api/topics/{topic_id}/survey/rounds")
    def survey_rounds(
        ctx: Context, topic_id: str, limit: int = Query(default=100, ge=1, le=1000)
    ) -> tuple[SurveyRound, ...]:
        require_topic(ctx, topic_id)
        return ctx.store.surveys.rounds(topic_id, limit=limit)
