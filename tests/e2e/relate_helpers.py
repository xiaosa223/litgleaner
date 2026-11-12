"""关系输入测试使用真实归档、当前提取、候选和数据库。"""

from dataclasses import replace
from pathlib import Path

from litgleaner.application.documents import save_document
from litgleaner.application.llm_gateway import Prompt, TaskRoute
from litgleaner.application.stages.extract import extraction_input_hash, extraction_producer_version
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.ports.store import FullTextState
from tests.e2e.memory_helpers import add, citation

HERE = "We solve the sparse sequence alignment problem with our recurrent model."
THERE = "Our attention method solves the sparse sequence alignment problem efficiently."
TAIL = "The secret tail provides an entirely different experimental conclusion."


def configure(harness, **settings):
    ctx = harness.ctx
    prompt = (Path(__file__).parents[2] / "prompts/relate.md").read_text()
    route = TaskRoute(harness.llm, "cheap", Prompt("relate", prompt), "test-provider")
    harness.ctx = replace(
        ctx,
        llm=replace(ctx.llm, routes={**ctx.llm.routes, "relate": route}),
        settings=replace(ctx.settings, relate=ctx.settings.relate.model_copy(update=settings)),
    )


def source(harness, paper_id, quote, *, blocks=None, fields=None, doc_key=None, **changes):
    ctx = harness.ctx
    previous = ctx.store.papers.get_extraction(paper_id)
    document = NormalizedDocument(
        1,
        paper_id.replace("-", "")[:64],
        f"relate-test-{previous.id if previous else 0}",
        None,
        tuple(blocks or [Block("text", quote + " " + TAIL, 2)]),
        (),
        "ok",
    )
    saved_key, _ = save_document(ctx, document)
    state = FullTextState(paper_id, "parsed", doc_key=doc_key or saved_key)
    ctx.store.papers.save_fulltext(state, ctx.now_iso())
    expected = extraction_input_hash(
        state.doc_key,
        ctx.settings.chunker,
        ctx.settings.reading_language,
        ctx.settings.assess_token_budget,
    )
    data = fields or {"problem": {"text": "Problem", "quote": quote, "page": 2, "verified": True}}
    ctx.store.papers.insert_extraction(
        paper_id,
        changes.get("input_hash", expected),
        changes.get("producer", extraction_producer_version(ctx)),
        data,
        ctx.now_iso(),
    )
    return state.doc_key


def pair(harness, **settings):
    configure(harness, **settings)
    a, b = add(harness, "relate-anchor"), add(harness, "relate-neighbor")
    source(harness, a, HERE)
    source(harness, b, THERE)
    citation(harness, a, target=b)
    return a, b


def answer(prepared, a, b, *, kind="uses_method_of", quote_a=HERE, quote_b=THERE):
    return {
        "paper_a": a,
        "paper_b": b,
        "kind": kind,
        "explanation": "The two original passages support this relation.",
        "evidence_a": {
            "paper_id": a,
            "doc_key": prepared.sources[a].doc_key,
            "page": 2,
            "quote": quote_a,
        },
        "evidence_b": {
            "paper_id": b,
            "doc_key": prepared.sources[b].doc_key,
            "page": 2,
            "quote": quote_b,
        },
    }
