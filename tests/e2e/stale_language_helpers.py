"""构造真实旧问答任务，继续核验缺少请求生产者的历史 unknown 合同。"""

from litgleaner.application import qa
from litgleaner.application.documents import load_document
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.qa import Question
from litgleaner.ports.store import JobSpec


def legacy_question(harness, paper_id):
    ctx = harness.ctx
    question = Question(
        "legacy-first",
        paper_id,
        "first",
        "Explain alpha evidence",
        None,
        None,
        ctx.settings.reading_language,
        ctx.now_iso(),
    )
    state = ctx.store.papers.get_fulltext(paper_id)
    ctx.store.qa.add_question(question)
    ctx.store.jobs.enqueue(
        JobSpec(
            qa.question_job_key(question.id),
            "ask",
            {
                "question_id": question.id,
                "doc_key": state.doc_key,
                "document_hash": stable_hash(load_document(ctx, state.doc_key).to_dict()),
                "chunker": ctx.settings.chunker.version,
            },
        ),
        ctx.now_iso(),
    )
    return question
