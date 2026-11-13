"""起草检索式与确认写入的接口（第 7.2 节，docs/TOPIC-DRAFT.md）。"""

from typing import Annotated, cast

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from litgleaner.application import topic_draft
from litgleaner.application.survey_plan import require_topic
from litgleaner.application.term_counts import TermCounts, TermRow, count_terms
from litgleaner.application.topic_draft import DraftPreview, DraftStatus, DraftView
from litgleaner.application.topic_setup import NEW
from litgleaner.domain.errors import ConfigError, InvalidRequest
from litgleaner.domain.glossary import GlossaryTerm
from litgleaner.domain.topic_draft import (
    MAX_EXCLUDE,
    MAX_GLOSSARY,
    MAX_INCLUDE,
    MAX_SYNONYMS,
    build_query,
)
from litgleaner.ports.topic_settings import TopicSettings
from litgleaner.server.runtime import Context

_Term = Annotated[str, StringConstraints(max_length=120)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfirmDraft(_Strict):
    plan_hash: str = Field(min_length=1, max_length=200)


class EmptyBody(_Strict):
    pass


class IncludeTerm(_Strict):
    term: _Term
    synonyms: list[_Term] = Field(default_factory=list, max_length=MAX_SYNONYMS)


class GlossaryEntry(_Strict):
    forms: list[_Term] = Field(min_length=1, max_length=8)
    translation: str = Field(min_length=1, max_length=120)
    definition: str | None = Field(default=None, max_length=300)


class QueryBody(_Strict):
    include: list[IncludeTerm] = Field(min_length=1, max_length=MAX_INCLUDE)
    exclude: list[_Term] = Field(default_factory=list, max_length=MAX_EXCLUDE)
    glossary: list[GlossaryEntry] = Field(default_factory=list, max_length=MAX_GLOSSARY)


class CountBody(_Strict):
    include: list[IncludeTerm] = Field(min_length=1, max_length=MAX_INCLUDE)
    exclude: list[_Term] = Field(default_factory=list, max_length=MAX_EXCLUDE)


class AppliedTopic(BaseModel):
    id: str
    terms: int
    status: str  # 保存后课题的状态；暂停的课题不会因保存而重新启用


def _settings(request: Request) -> TopicSettings:
    settings = cast(TopicSettings | None, request.app.state.topic_settings)
    if settings is None:
        raise InvalidRequest("topic_settings_unavailable")
    return settings


def _glossary(entries: list[GlossaryEntry]) -> tuple[GlossaryTerm, ...]:
    try:
        return tuple(
            GlossaryTerm(
                forms=tuple(e.forms), translation=e.translation.strip(), definition=e.definition
            )
            for e in entries
        )
    except ValueError:
        raise InvalidRequest("glossary_invalid") from None


def install_topic_draft_routes(app: FastAPI) -> None:
    @app.post("/api/topics/{topic_id}/draft/preview")
    def draft_preview(ctx: Context, topic_id: str, body: EmptyBody | None = None) -> DraftPreview:
        return topic_draft.preview(ctx, topic_id)

    @app.post("/api/topics/{topic_id}/draft")
    def draft_queue(ctx: Context, topic_id: str, body: ConfirmDraft) -> DraftStatus:
        return topic_draft.queue(ctx, topic_id, body.plan_hash)

    @app.get("/api/topics/{topic_id}/draft")
    def draft_view(ctx: Context, topic_id: str) -> DraftView:
        return topic_draft.view(ctx, topic_id)

    @app.get("/api/topics/{topic_id}/draft/jobs/{job_key}")
    def draft_status(ctx: Context, topic_id: str, job_key: str) -> DraftStatus:
        return topic_draft.status(ctx, topic_id, job_key)

    @app.post("/api/topics/{topic_id}/term-counts")
    def term_counts(ctx: Context, topic_id: str, body: CountBody) -> TermCounts:
        """按编辑表里还没保存的检索词查，不调用模型；每行一次来源检索请求。"""
        require_topic(ctx, topic_id)
        rows = tuple(TermRow(i.term, tuple(i.synonyms)) for i in body.include)
        return count_terms(ctx, rows, tuple(body.exclude))

    @app.put("/api/topics/{topic_id}/query")
    def apply_query(ctx: Context, request: Request, topic_id: str, body: QueryBody) -> AppliedTopic:
        require_topic(ctx, topic_id)
        try:
            query = build_query([(i.term, i.synonyms) for i in body.include], body.exclude)
        except ValueError:
            raise InvalidRequest("query_invalid") from None
        try:
            topic = _settings(request).apply_query(topic_id, query, _glossary(body.glossary))
        except ConfigError:
            raise HTTPException(500, "topic_save_failed") from None
        state = ctx.store.judgments.get_topic_state(topic_id)
        status = state.status if state else NEW
        return AppliedTopic(id=topic.id, terms=len(topic.query.terms), status=status)
