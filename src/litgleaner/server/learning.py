"""导读、回答卡片、掌握程度和复习的接口（第 12.7 节）。调用模型的操作都需要用户显式确认。"""

from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI, Query
from pydantic import Field

from litgleaner.application import answers, concept_links, guide, learning
from litgleaner.domain.errors import NotFound
from litgleaner.domain.mastery import MasteryState
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import _Body


class GuideBody(_Body):
    plan_hash: str = Field(min_length=1, max_length=200)


class AnswerBody(_Body):
    question_index: int = Field(ge=0, le=2)
    text: str = Field(min_length=1, max_length=2000)
    evaluate: bool = True


class WrongBody(_Body):
    reason: str = Field(default="", max_length=500)


class LevelBody(_Body):
    level: int = Field(ge=0, le=3)


class DecisionBody(_Body):
    target: str = Field(min_length=1, max_length=100)
    decision: Literal["confirmed", "rejected"]


class ReviewBody(_Body):
    rating: Literal["remembered", "fuzzy", "forgot"]


@dataclass(frozen=True)
class GuidePage:
    view: guide.GuideView
    cards: tuple[answers.CardView, ...]
    evaluate_configured: bool
    evaluate_estimated_usd: float | None


def _page(ctx: Context, paper_id: str) -> GuidePage:
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound("论文不存在")
    view = guide.view(ctx, paper_id)
    cards = answers.cards(ctx, paper_id, view.guide.id) if view.guide else ()
    return GuidePage(
        view,
        cards,
        answers.TASK in ctx.llm.routes,
        ctx.store.runs.average_llm_cost(answers.TASK),
    )


def install_learning_routes(app: FastAPI) -> None:
    _install_guide(app)
    _install_knowledge(app)


def _install_guide(app: FastAPI) -> None:
    @app.get("/api/papers/{paper_id}/guide")
    def read_guide(ctx: Context, paper_id: str) -> GuidePage:
        return _page(ctx, paper_id)

    @app.post("/api/papers/{paper_id}/guide", status_code=202)
    def generate_guide(ctx: Context, paper_id: str, body: GuideBody) -> GuidePage:
        guide.queue(ctx, paper_id, body.plan_hash)
        return _page(ctx, paper_id)

    @app.post("/api/papers/{paper_id}/guide/{guide_id}/answers", status_code=201)
    def submit_answer(ctx: Context, paper_id: str, guide_id: str, body: AnswerBody) -> GuidePage:
        answers.submit(
            ctx, paper_id, guide_id, body.question_index, body.text, evaluate=body.evaluate
        )
        return _page(ctx, paper_id)

    @app.post("/api/papers/{paper_id}/answers/{card_id}/evaluate", status_code=202)
    def evaluate(ctx: Context, paper_id: str, card_id: str) -> GuidePage:
        answers.evaluate(ctx, paper_id, card_id)
        return _page(ctx, paper_id)

    @app.post("/api/papers/{paper_id}/answers/{card_id}/wrong", status_code=204)
    def mark_wrong(ctx: Context, paper_id: str, card_id: str, body: WrongBody) -> None:
        answers.mark_wrong(ctx, paper_id, card_id, body.reason)


def _install_knowledge(app: FastAPI) -> None:
    @app.get("/api/knowledge/cards")
    def concept_cards(
        ctx: Context,
        q: str = Query(default="", max_length=500),
        topic_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> learning.ConceptCards:
        return learning.concept_cards(ctx, topic_id=topic_id, query=q, limit=limit, offset=offset)

    @app.post("/api/knowledge/concepts/{concept_id}/level")
    def set_level(ctx: Context, concept_id: str, body: LevelBody) -> MasteryState:
        return learning.set_level(ctx, concept_id, body.level)

    @app.post("/api/knowledge/concepts/{concept_id}/review")
    def review(ctx: Context, concept_id: str, body: ReviewBody) -> MasteryState:
        return learning.review(ctx, concept_id, body.rating)

    @app.get("/api/knowledge/review")
    def due(
        ctx: Context, limit: int = Query(default=20, ge=1, le=100)
    ) -> tuple[learning.ConceptCard, ...]:
        return learning.due_cards(ctx, limit)

    @app.get("/api/knowledge/merges")
    def merges(ctx: Context) -> tuple[concept_links.MergeView, ...]:
        return concept_links.pending_merges(ctx)

    @app.post("/api/knowledge/decisions", status_code=204)
    def decide(ctx: Context, body: DecisionBody) -> None:
        concept_links.decide(ctx, body.target, body.decision)
