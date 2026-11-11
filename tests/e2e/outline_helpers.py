"""综述验收的已读本地来源与冻结事实；不通过真实网络创建论文。"""

from litgleaner.application import outline
from litgleaner.application.pipeline import run_on_demand
from litgleaner.application.stages.discover import resolve_record
from litgleaner.application.stages.extract import (
    extraction_input_hash,
    extraction_producer_version,
)
from litgleaner.domain.paper import SourceRecord
from litgleaner.ports.store import FullTextState, TriageResult
from tests.e2e.conftest import Harness

TOPIC = "alpha-topic"


def setup_outline(harness: Harness, count: int = 3):
    harness.edit_yaml(
        "llm.yaml",
        lambda data: data["tasks"].update(
            {
                "outline": {"provider": "main", "model": "outline-model", "prompt": "outline"},
            }
        ),
    )
    ctx = harness.rebuild()
    ids = tuple(add_read_paper(harness, f"Alpha read {n}") for n in range(count))
    return ctx, tuple(sorted(ids))


def add_read_paper(
    harness: Harness,
    title: str,
    *,
    relevant: bool = True,
    read: bool = True,
    evidence: bool = True,
    evidence_data: dict | None = None,
):
    ctx = harness.ctx
    id_ = resolve_record(
        ctx.store.papers,
        SourceRecord(
            title=title,
            abstract="alpha research",
            doi=f"10.1000/{title.replace(' ', '-').lower()}",
        ),
        ctx.now_iso(),
    )
    topic = ctx.topics[TOPIC]
    ctx.store.judgments.save_triage(
        TriageResult(id_, TOPIC, topic.topic_hash, relevant, "fixture", "fixture", "fixture"),
        ctx.now_iso(),
    )
    ctx.store.reader.set_state(id_, "read" if read else "reading", {}, ctx.now_iso())
    if evidence:
        doc_key = f"blob://doc/{id_}.json"
        ctx.store.papers.save_fulltext(FullTextState(id_, "parsed", doc_key=doc_key), ctx.now_iso())
        quote = {"text": "Alpha method", "quote": "We use alpha.", "page": 0, "verified": True}
        ctx.store.papers.insert_extraction(
            id_,
            extraction_input_hash(
                doc_key,
                ctx.settings.chunker,
                ctx.settings.reading_language,
                ctx.settings.assess_token_budget,
            ),
            extraction_producer_version(ctx),
            evidence_data
            if evidence_data is not None
            else {"method": quote, "datasets": [{**quote, "text": "Alpha dataset"}]},
            ctx.now_iso(),
        )
    return id_


def generate(ctx):
    plan = outline.preview(ctx, TOPIC)
    queued = outline.queue(ctx, TOPIC, plan.plan_hash)
    assert run_on_demand(ctx).ok
    return outline.status(ctx, TOPIC, queued.job_key)
