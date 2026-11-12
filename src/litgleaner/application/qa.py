"""用户确认的论文问答：幂等排队、限定原文上下文、核验后保存回答。"""

from collections.abc import Iterable, Mapping
from typing import Any

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_chunks, load_document
from litgleaner.application.qa_context import (
    QAContext,
    build_inputs,
    qa_token_count,
    verified_statements,
)
from litgleaner.application.qa_memory_input import prepare
from litgleaner.application.qa_memory_run import process as process_memory
from litgleaner.application.qa_schema import ANSWER_SCHEMA
from litgleaner.application.stages.base import Stage
from litgleaner.application.topics import active_topics
from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.errors import InvalidRequest, NotFound, PermanentError
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.domain.qa import Answer, Question
from litgleaner.ports.store import JobSpec

TASK = "ask"


def question_job_key(question_id: str) -> str:
    return f"{TASK}:{question_id}"


def _document(ctx: AppContext, paper_id: str) -> tuple[str, NormalizedDocument]:
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound(f"论文不存在：{paper_id}")
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is None or state.doc_key is None:
        raise InvalidRequest("这篇论文还没有解析出的正文，无法问答")
    return state.doc_key, load_document(ctx, state.doc_key)


def _same_request(
    question: Question, text: str, selection: str | None, page: int | None
) -> Question:
    if (question.text, question.selection, question.page) != (text, selection, page):
        raise InvalidRequest("同一个 request_id 已用于不同的问题内容")
    return question


def queue(
    ctx: AppContext,
    paper_id: str,
    text: str,
    selection: str | None = None,
    page: int | None = None,
    *,
    request_id: str = "",
) -> Question:
    if existing := ctx.store.qa.by_request(paper_id, request_id):
        return _same_request(existing, text, selection, page)
    if not request_id.strip() or len(request_id) > 200 or not text.strip() or len(text) > 6000:
        raise InvalidRequest("问题与 request_id 不能为空，且不得超过长度限制")
    if selection is not None and len(selection) > 6000:
        raise InvalidRequest("选区过长，请缩小到需要解释的段落")
    if qa_token_count(text + " " + (selection or "")) > 1600:
        raise InvalidRequest("问题和选区过长，请缩小范围后提问")
    question = Question(
        str(ULID()),
        paper_id,
        request_id,
        text,
        selection,
        page,
        ctx.settings.reading_language,
        ctx.now_iso(),
    )
    context = prepare(ctx, question)
    spec = JobSpec(
        question_job_key(question.id),
        TASK,
        {
            "question_id": question.id,
            "doc_key": context["doc_key"],
            "document_hash": context["document_hash"],
            "chunker": ctx.settings.chunker.version,
            "context": context,
        },
    )
    with ctx.store.atomic():
        if not ctx.store.qa.add_question(question):
            existing = ctx.store.qa.by_request(paper_id, request_id)
            if existing is None:
                raise RuntimeError("问答记录冲突后无法读取原问题")
            return _same_request(existing, text, selection, page)
        ctx.store.jobs.enqueue(spec, ctx.now_iso())
    return question


def history(ctx: AppContext, paper_id: str, limit: int = 50) -> tuple[Question, ...]:
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound(f"论文不存在：{paper_id}")
    return ctx.store.qa.questions(paper_id, min(max(limit, 1), 100))


def answer(ctx: AppContext, question_id: str) -> Answer | None:
    if ctx.store.qa.question(question_id) is None:
        raise NotFound(f"问题不存在：{question_id}")
    return ctx.store.qa.answer(question_id)


def _profile(ctx: AppContext) -> str:
    return "\n\n".join(topic.profile_text() for topic in active_topics(ctx).values())[:500]


def _inputs(
    ctx: AppContext, question: Question, doc_key: str, document: NormalizedDocument
) -> tuple[QAContext, str]:
    route = ctx.llm.require_route(TASK)
    system = route.prompt.render({"output_language": language_name(question.reading_language)})
    return build_inputs(question, document, load_chunks(ctx, doc_key), _profile(ctx), system)


class AskStage(Stage):
    name = TASK

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        question_id = str(payload["question_id"])
        if ctx.store.qa.answer(question_id) is not None:
            return
        question = ctx.store.qa.question(question_id)
        if question is None:
            raise PermanentError("问答任务的问题不存在")
        if "context" in payload:
            context_data = payload["context"]
            if not isinstance(context_data, Mapping):
                raise PermanentError("问答冻结来源缺失，请重新提问")
            process_memory(ctx, question, context_data)
            return
        doc_key, document = _document(ctx, question.paper_id)
        expected = (doc_key, stable_hash(document.to_dict()), ctx.settings.chunker.version)
        planned = (payload.get("doc_key"), payload.get("document_hash"), payload.get("chunker"))
        if question.reading_language != ctx.settings.reading_language or expected != planned:
            raise PermanentError("阅读语言、原文或切分规则已经变化，请重新提问")
        context, inputs = _inputs(ctx, question, doc_key, document)
        result = ctx.llm.call(
            TASK,
            {"output_language": language_name(question.reading_language)},
            inputs,
            ANSWER_SCHEMA,
            paper_id=question.paper_id,
        )
        ctx.store.qa.add_answer(
            Answer(
                str(ULID()),
                question.paper_id,
                question.id,
                verified_statements(
                    question.paper_id, document, context, result.data["statements"]
                ),
                result.model,
                result.prompt_version,
                stable_hash(inputs),
                ctx.now_iso(),
            )
        )
