"""论文问答接口：提交后排队，历史和状态查询不调用模型。"""

from dataclasses import dataclass
from typing import Literal

from fastapi import FastAPI, Query
from pydantic import Field

from litgleaner.application import qa, reader
from litgleaner.application.context import AppContext
from litgleaner.domain.errors import BudgetExceeded, InvalidRequest, NotFound, TaskBudgetExceeded
from litgleaner.domain.qa import Answer, Question
from litgleaner.domain.reading import Annotation, NewAnnotation
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import _Body


class AskBody(_Body):
    text: str = Field(min_length=1, max_length=4000)
    selection: str | None = Field(default=None, min_length=1, max_length=8000)
    page: int | None = Field(default=None, ge=0, le=100_000)
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


@dataclass(frozen=True)
class QAView:
    question: Question
    answer: Answer | None
    state: Literal[
        "queued", "retrying", "done", "failed", "configuration_required", "budget_exceeded"
    ]
    code: str | None = None


def _question(ctx: AppContext, paper_id: str, question_id: str) -> Question:
    question = ctx.store.qa.question(question_id)
    if question is None or question.paper_id != paper_id:
        raise NotFound("问答记录不存在")
    return question


def _view(ctx: AppContext, question: Question) -> QAView:
    answer = ctx.store.qa.answer(question.id)
    if answer is not None:
        return QAView(question, answer, "done")
    job = ctx.store.jobs.get(qa.question_job_key(question.id))
    if job is None or job.status in {"dead", "done"}:
        return QAView(question, None, "failed", "question_failed")
    if "ask" not in ctx.llm.routes:
        return QAView(question, None, "configuration_required", "model_configuration_required")
    try:
        ctx.llm.check_budget("ask")
    except (BudgetExceeded, TaskBudgetExceeded):
        return QAView(question, None, "budget_exceeded", "budget_exceeded")
    return QAView(question, None, "retrying" if job.attempts else "queued")


def install_qa_routes(app: FastAPI) -> None:
    @app.get("/api/papers/{paper_id}/qa")
    def history(
        ctx: Context, paper_id: str, limit: int = Query(default=50, ge=1, le=200)
    ) -> list[QAView]:
        if ctx.store.papers.get(paper_id) is None:
            raise NotFound("论文不存在")
        return [_view(ctx, item) for item in ctx.store.qa.questions(paper_id, limit)]

    @app.post("/api/papers/{paper_id}/qa", status_code=202)
    def ask(ctx: Context, paper_id: str, body: AskBody) -> QAView:
        question = qa.queue(
            ctx,
            paper_id,
            body.text,
            selection=body.selection,
            page=body.page,
            request_id=body.request_id,
        )
        return _view(ctx, question)

    @app.get("/api/papers/{paper_id}/qa/{question_id}")
    def question(ctx: Context, paper_id: str, question_id: str) -> QAView:
        return _view(ctx, _question(ctx, paper_id, question_id))

    @app.post("/api/papers/{paper_id}/qa/{question_id}/annotation", status_code=201)
    def annotate(ctx: Context, paper_id: str, question_id: str) -> Annotation:
        question = _question(ctx, paper_id, question_id)
        answer = ctx.store.qa.answer(question_id)
        if answer is None:
            raise InvalidRequest("请等待回答完成后再保存为批注")
        citations = [
            citation
            for statement in answer.statements
            for citation in statement.citations
            if citation.paper_id == paper_id
        ]
        page = citations[0].page if citations else question.page
        quote = citations[0].quote if citations else None
        item = NewAnnotation(
            paper_id, "note", page, None, quote, question.text, qa_question_id=question.id
        )
        return reader.add_annotation(ctx, item)
