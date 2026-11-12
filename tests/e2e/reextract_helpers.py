"""历史重提取的真实归档、历史事实与离线模型响应。"""

from dataclasses import replace

from sqlalchemy import text

from litgleaner.application import reextract
from litgleaner.application.documents import load_document, save_document
from litgleaner.application.pipeline import run_on_demand
from litgleaner.application.stages.discover import resolve_record
from litgleaner.application.stages.extract import extraction_input_hash, extraction_producer_version
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper import SourceRecord
from litgleaner.ports.llm import LLMResponse
from litgleaner.ports.store import FullTextState
from tests.e2e.conftest import extraction_answer

QUOTE = "Transformer provides a flexible representation of the experimental evidence."


def ready(harness, name="Historical paper", *, current=False, history=True, state="parsed"):
    ctx = harness.ctx
    paper_id = resolve_record(ctx.store.papers, SourceRecord(title=name), ctx.now_iso())
    document = NormalizedDocument(
        1,
        stable_hash(name),
        "reextract-test",
        None,
        (Block("heading", "Abstract", 0), Block("text", QUOTE, 0)),
        (),
        "ok",
    )
    key, _ = save_document(ctx, document)
    ctx.store.papers.save_fulltext(FullTextState(paper_id, state, doc_key=key), ctx.now_iso())
    if history:
        ctx.store.papers.insert_extraction(
            paper_id,
            input_hash(ctx, key) if current else "old-input",
            extraction_producer_version(ctx) if current else "old-producer",
            {"summary": "historical summary"},
            ctx.now_iso(),
        )
    return paper_id


def input_hash(ctx, key):
    return extraction_input_hash(
        key, ctx.settings.chunker, ctx.settings.reading_language, ctx.settings.assess_token_budget
    )


def respond(harness, monkeypatch, *, hook=None, error=None, concepts=True):
    requests = []

    def complete(request):
        requests.append(request)
        if hook:
            hook()
        if error:
            raise error
        data = extraction_answer(
            summary="fresh summary",
            problem={"text": "Representation", "quote": QUOTE, "page": 0},
            key_concepts=[
                {
                    "name": "Transformer",
                    "role": "core",
                    "page": 0,
                    "definition": QUOTE,
                }
            ]
            if concepts
            else [],
        )
        return LLMResponse(data, "response-alias", 50, 30, 0.125)

    monkeypatch.setattr(harness.ctx.llm.routes["extract"].llm, "complete", complete)
    return requests


def queued(harness, ids=None):
    plan = reextract.preview(harness.ctx, ids)
    return reextract.queue(harness.ctx, plan.plan_hash, ids)


def consume(harness):
    return next(row for row in run_on_demand(harness.ctx).stages if row.name == "reextract")


def change(harness, kind, paper_id):
    ctx = harness.ctx
    if kind in {"prompt", "model", "provider"}:
        route = ctx.llm.routes["extract"]
        changes = {
            "prompt": {"prompt": replace(route.prompt, text=route.prompt.text + "\nV2")},
            "model": {"model": "different-request-model"},
            "provider": {"provider_version": "v2"},
        }
        ctx.llm.routes["extract"] = replace(route, **changes[kind])
    elif kind in {"language", "budget", "chunker"}:
        changes = {
            "language": {"reading_language": "en"},
            "budget": {"assess_token_budget": ctx.settings.assess_token_budget + 10},
            "chunker": {"chunker": replace(ctx.settings.chunker, target_tokens=500)},
        }
        object.__setattr__(ctx, "settings", replace(ctx.settings, **changes[kind]))
    elif kind == "metadata":
        ctx.store._db.write(
            text("UPDATE paper SET title='Changed title' WHERE id=:id").bindparams(id=paper_id)
        )
    elif kind == "body":
        state = ctx.store.papers.get_fulltext(paper_id)
        document = load_document(ctx, state.doc_key)
        save_document(
            ctx, replace(document, blocks=(Block("text", "Changed actual evidence.", 0),))
        )
    elif kind == "baseline":
        ctx.store.papers.insert_extraction(
            paper_id, "concurrent-input", "concurrent-producer", {}, ctx.now_iso()
        )
    elif kind == "pointer":
        state = ctx.store.papers.get_fulltext(paper_id)
        ctx.store.papers.save_fulltext(replace(state, doc_key="missing-pointer"), ctx.now_iso())
