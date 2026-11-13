"""课题：新建、列表、阅读路线和调研接口。"""

from typing import Annotated, cast

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from litgleaner.application.reading_route import ReadingRoute, reading_route
from litgleaner.application.survey import SurveyStatus, get_preview, preview, queue, status
from litgleaner.application.survey_plan import require_topic
from litgleaner.application.topic_setup import MAX_GOALS, MAX_NAME, TopicSummary, topic_summaries
from litgleaner.domain.errors import ConfigError, InvalidRequest
from litgleaner.domain.survey import SurveyPreview, SurveyRound
from litgleaner.ports.topic_settings import TopicFields, TopicSettings
from litgleaner.server.runtime import Context
from litgleaner.server.topic_drafts import install_topic_draft_routes
from litgleaner.server.topic_maps import install_topic_map_routes
from litgleaner.server.topic_settings import install_topic_settings_routes


class ConfirmSurvey(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(min_length=1, max_length=200)


class PreviewSurvey(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_EMPTY_PREVIEW = PreviewSurvey()
_Text = Annotated[str, StringConstraints(max_length=8000)]


class NewTopic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=MAX_NAME)
    background: _Text
    content: _Text
    goals: list[Annotated[str, StringConstraints(max_length=1000)]] = Field(
        min_length=1, max_length=MAX_GOALS
    )

    def fields(self) -> TopicFields:
        return TopicFields(self.name, self.background, self.content, tuple(self.goals))


class CreatedTopic(BaseModel):
    id: str


def _topic_settings(request: Request) -> TopicSettings:
    settings = cast(TopicSettings | None, request.app.state.topic_settings)
    if settings is None:
        raise InvalidRequest("topic_settings_unavailable")
    return settings


def install_topic_routes(app: FastAPI) -> None:
    install_topic_map_routes(app)
    install_topic_draft_routes(app)
    install_topic_settings_routes(app)

    @app.get("/api/topics")
    def topics(ctx: Context) -> tuple[TopicSummary, ...]:
        return topic_summaries(ctx)

    @app.post("/api/topics")
    def create_topic(request: Request, body: NewTopic) -> CreatedTopic:
        try:
            return CreatedTopic(id=_topic_settings(request).create(body.fields()).id)
        except ConfigError:  # 写文件失败是服务端的问题；通用处理器的文案是模型设置的
            raise HTTPException(500, "topic_save_failed") from None

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
