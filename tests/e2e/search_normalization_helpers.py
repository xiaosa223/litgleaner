"""规范搜索测试保留真实 Unicode 原文、归档及规范源身份。"""

from litgleaner.application.documents import save_document
from litgleaner.application.stages.discover import resolve_record
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.knowledge import ConceptOccurrence
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.qa import Answer, Question, Statement
from litgleaner.domain.reading import NewAnnotation
from litgleaner.ports.store import FullTextState

TAIL = "Straße Cafe\u0301 神经网络"


def paper(harness, title="Local original paper", *, library=False):
    id_ = resolve_record(harness.ctx.store.papers, SourceRecord(title=title), harness.ctx.now_iso())
    if library:
        harness.ctx.store.reader.set_state(id_, "queued", None, harness.ctx.now_iso())
    return id_


def document(harness, id_, content, page=7):
    doc = NormalizedDocument(
        1,
        stable_hash(id_),
        "search-normalization",
        None,
        (Block("text", content, page),),
        (),
        "ok",
    )
    key, _ = save_document(harness.ctx, doc)
    harness.ctx.store.papers.save_fulltext(
        FullTextState(id_, "parsed", doc_key=key), harness.ctx.now_iso()
    )
    harness.ctx.store.search.replace_document(id_, key, doc.blocks)
    return key


def qa(harness, id_):
    ctx = harness.ctx
    question = Question(
        "normalize-question",
        id_,
        "normalize-request",
        "ＱＵＥＳＴＩＯＮ " + TAIL,
        None,
        None,
        "zh-CN",
        ctx.now_iso(),
    )
    ctx.store.qa.add_question(question)
    ctx.store.qa.add_answer(
        Answer(
            "normalize-answer",
            id_,
            question.id,
            (Statement("ＡＮＳＷＥＲ " + TAIL, (), True),),
            "fake",
            "prompt",
            "input",
            ctx.now_iso(),
        )
    )
    return question


def corpus(harness):
    ctx = harness.ctx
    id_ = paper(harness, "ＰＡＰＥＲ " + TAIL, library=True)
    key = document(harness, id_, "ＦＵＬＬＴＥＸＴ " + TAIL)
    note = ctx.store.reader.add_annotation(
        NewAnnotation(id_, "note", 3, None, "ＱＵＯＴＥ " + TAIL, "ＮＯＴＥ " + TAIL),
        ctx.now_iso(),
    )
    ctx.store.papers.insert_extraction(
        id_, "input", "producer", {"summary": "ＥＸＴＲＡＣＴ " + TAIL}, ctx.now_iso()
    )
    question = qa(harness, id_)
    concept = ctx.store.knowledge.register("ＣＯＮＣＥＰＴ " + TAIL, "en", ctx.now_iso())
    ctx.store.knowledge.add_occurrence(
        ConceptOccurrence(
            concept.id,
            id_,
            "core",
            "ＤＥＦＩＮＩＴＩＯＮ " + TAIL,
            4,
            None,
            True,
            "input",
            "producer",
            ctx.now_iso(),
        )
    )
    return id_, key, note, question, concept


def forbidden(*_args, **_kwargs):
    raise AssertionError("local search must not write, infer, fetch, embed or enqueue")


def block_actions(harness, monkeypatch):
    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    for route in harness.ctx.llm.routes.values():
        monkeypatch.setattr(route.llm, "complete", forbidden)
        monkeypatch.setattr(route.llm, "stream", forbidden)
    for provider in harness.ctx.sources.values():
        monkeypatch.setattr(provider, "fetch", forbidden)
    if harness.ctx.embedder is not None:
        monkeypatch.setattr(harness.ctx.embedder, "embed", forbidden)
