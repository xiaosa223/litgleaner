"""只执行已确认的冻结问答，调用前后核验同一组来源并保存一次答案。"""

from collections.abc import Mapping
from typing import Any

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.application.qa_memory_evidence import verified_statements
from litgleaner.application.qa_memory_state import check, state_token
from litgleaner.application.qa_schema import ANSWER_SCHEMA
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.errors import PermanentError
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.domain.qa import Answer, Question


def _checked(ctx: AppContext, question: Question, manifest: Mapping[str, Any]) -> StaleInputs:
    basis = StaleInputs(ctx, ctx.store.stale_snapshot())
    reasons, unchecked = check(basis, question, manifest)
    if reasons or unchecked:
        raise PermanentError("；".join((*reasons, *unchecked, "请重新提问")))
    return basis


def process(ctx: AppContext, question: Question, manifest: Mapping[str, Any]) -> None:
    _checked(ctx, question, manifest)
    result = ctx.llm.call(
        "ask",
        {"output_language": language_name(question.reading_language)},
        manifest["inputs"],
        ANSWER_SCHEMA,
        paper_id=question.paper_id,
    )
    basis = _checked(ctx, question, manifest)
    answer = Answer(
        str(ULID()),
        question.paper_id,
        question.id,
        verified_statements(question, manifest, basis, result.data["statements"]),
        result.model,
        result.prompt_version,
        stable_hash(manifest["inputs"]),
        ctx.now_iso(),
        dict(manifest),
        str(manifest["producer"]),
    )
    token = state_token(basis, manifest)
    with ctx.store.atomic():
        current = StaleInputs(ctx, ctx.store.stale_snapshot())
        if state_token(current, manifest) != token:
            raise PermanentError("问答使用的论文、关系或笔记已经变化，请重新提问")
        ctx.store.qa.add_answer(answer)
