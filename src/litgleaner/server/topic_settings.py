"""课题设置：查看和修改研究画像、启用、暂停、回填历史论文（第 7.1、7.3、12.8 节）。

与命令行 topic activate / pause / backfill 等价；都不调用模型，回填只在确认后排队初筛。
"""

from typing import Annotated, cast

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from litgleaner.application.survey_plan import require_topic
from litgleaner.application.topic_setup import MAX_GOALS, MAX_NAME, NEW
from litgleaner.application.topics import BackfillPlan, preview_backfill, queue_backfill
from litgleaner.domain.errors import ConfigError, InvalidRequest
from litgleaner.domain.topic import Topic, TopicGoal
from litgleaner.ports.topic_settings import GoalText, TopicProfile, TopicSettings
from litgleaner.server.runtime import Context

_Text = Annotated[str, StringConstraints(max_length=8000)]


class TopicDetail(BaseModel):
    id: str
    name: str
    background: str
    content: str
    goals: tuple[TopicGoal, ...]
    status: str
    terms: int


class GoalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Annotated[str, StringConstraints(max_length=40)] | None = None
    text: Annotated[str, StringConstraints(max_length=1000)]


class ProfileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=MAX_NAME)
    background: _Text
    content: _Text
    goals: list[GoalBody] = Field(min_length=1, max_length=MAX_GOALS)

    def profile(self) -> TopicProfile:
        goals = tuple(GoalText(goal.id, goal.text) for goal in self.goals)
        return TopicProfile(self.name, self.background, self.content, goals)


class TopicStatus(BaseModel):
    id: str
    status: str


class SavedProfile(BaseModel):
    topic: TopicDetail  # 新目标已有编号，界面直接换成它
    changed: bool  # 影响判断的部分变了：之后的新论文按新定义初筛，历史论文要回填


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfirmBackfill(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: str = Field(min_length=1, max_length=200)


class QueuedBackfill(BaseModel):
    queued: int


_EMPTY = EmptyBody()


def _status(ctx: Context, topic_id: str) -> str:
    state = ctx.store.judgments.get_topic_state(topic_id)
    return state.status if state else NEW


def _detail(ctx: Context, topic: Topic) -> TopicDetail:
    return TopicDetail(
        id=topic.id,
        name=topic.name,
        background=topic.background,
        content=topic.content,
        goals=topic.goals,
        status=_status(ctx, topic.id),
        terms=len(topic.query.terms),
    )


def _settings(request: Request) -> TopicSettings:
    settings = cast(TopicSettings | None, request.app.state.topic_settings)
    if settings is None:
        raise InvalidRequest("topic_settings_unavailable")
    return settings


def install_topic_settings_routes(app: FastAPI) -> None:
    @app.get("/api/topics/{topic_id}")
    def detail(ctx: Context, topic_id: str) -> TopicDetail:
        return _detail(ctx, require_topic(ctx, topic_id))

    @app.put("/api/topics/{topic_id}/profile")
    def update_profile(
        ctx: Context, request: Request, topic_id: str, body: ProfileBody
    ) -> SavedProfile:
        require_topic(ctx, topic_id)
        try:
            change = _settings(request).update_profile(topic_id, body.profile())
        except ConfigError:
            raise HTTPException(500, "topic_save_failed") from None
        return SavedProfile(topic=_detail(ctx, change.topic), changed=change.changed)

    @app.post("/api/topics/{topic_id}/activate")
    def activate(
        ctx: Context, request: Request, topic_id: str, body: EmptyBody = _EMPTY
    ) -> TopicStatus:
        require_topic(ctx, topic_id)
        _settings(request).set_tracking(topic_id, active=True)
        return TopicStatus(id=topic_id, status=_status(ctx, topic_id))

    @app.post("/api/topics/{topic_id}/pause")
    def pause(
        ctx: Context, request: Request, topic_id: str, body: EmptyBody = _EMPTY
    ) -> TopicStatus:
        require_topic(ctx, topic_id)
        _settings(request).set_tracking(topic_id, active=False)
        return TopicStatus(id=topic_id, status=_status(ctx, topic_id))

    @app.post("/api/topics/{topic_id}/backfill/preview")
    def backfill_preview(ctx: Context, topic_id: str, body: EmptyBody = _EMPTY) -> BackfillPlan:
        require_topic(ctx, topic_id)
        return preview_backfill(ctx, topic_id)

    @app.post("/api/topics/{topic_id}/backfill")
    def backfill(ctx: Context, topic_id: str, body: ConfirmBackfill) -> QueuedBackfill:
        require_topic(ctx, topic_id)
        return QueuedBackfill(queued=queue_backfill(ctx, topic_id, body.plan))
