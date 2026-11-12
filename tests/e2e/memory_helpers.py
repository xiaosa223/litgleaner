"""论文记忆离线验收：真实数据库里的用户文库与已核验提取。"""

from dataclasses import replace

from litgleaner.application.stages.discover import resolve_record
from litgleaner.application.stages.extract import extraction_input_hash, extraction_producer_version
from litgleaner.domain.citation import Citation
from litgleaner.domain.knowledge import ConceptOccurrence
from litgleaner.domain.paper import SourceRecord
from litgleaner.ports.store import FullTextState

QUOTE = "Our method uses this dataset in all experiments."


def add(harness, name, *, library=True):
    ctx = harness.ctx
    paper_id = resolve_record(
        ctx.store.papers, SourceRecord(title=name, doi=f"10.1000/{name}"), ctx.now_iso()
    )
    if library:
        ctx.store.reader.set_state(paper_id, "queued", None, ctx.now_iso())
    return paper_id


def citation(harness, citing, *, target=None, aliases=(), source="parsed", position="ref:0"):
    item = Citation(
        citing,
        position,
        "Original reference entry",
        source,
        0,
        target,
        "Reference title",
        tuple(aliases),
        ("We use the referenced method.",),
        ("methodology",),
        True,
        harness.ctx.now_iso(),
        3,
        (10, 20, 30, 40),
    )
    harness.ctx.store.citations.add(item)
    return item


def extract(harness, paper_id, *, data=None, core="Transformer", **changes):
    ctx = harness.ctx
    state = FullTextState(paper_id, "parsed", doc_key=f"blob://doc/{paper_id}")
    ctx.store.papers.save_fulltext(state, ctx.now_iso())
    expected = extraction_input_hash(
        state.doc_key,
        ctx.settings.chunker,
        ctx.settings.reading_language,
        ctx.settings.assess_token_budget,
    )
    version = extraction_producer_version(ctx)
    evidence = {"text": "Dataset X", "quote": QUOTE, "page": 2, "verified": True}
    fields = (
        data
        if data is not None
        else {
            "datasets": [evidence],
            "method": {**evidence, "text": "Attention"},
            "key_concepts": [{"name": core, "role": "core", "page": 1}],
        }
    )
    ctx.store.papers.insert_extraction(
        paper_id,
        changes.get("input_hash", expected),
        changes.get("producer", version),
        fields,
        ctx.now_iso(),
    )
    if core:
        concept = ctx.store.knowledge.register(core, None, ctx.now_iso())
        ctx.store.knowledge.add_occurrence(
            ConceptOccurrence(
                concept.id, paper_id, "core", None, 1, None, False, expected, version, ctx.now_iso()
            )
        )
    return ctx.store.papers.get_extraction(paper_id)


def no_extract_route(harness):
    ctx = harness.ctx
    harness.ctx = replace(
        ctx,
        llm=replace(ctx.llm, routes={k: v for k, v in ctx.llm.routes.items() if k != "extract"}),
    )
