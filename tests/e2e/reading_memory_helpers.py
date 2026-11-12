"""阅读记忆测试的规范文档、真实核实批次和用户反馈，不调用生成模型。"""

import json

from litgleaner.application.relate_input import configuration, producer_version, source_hash
from litgleaner.application.relation_feedback import apply
from litgleaner.domain.paper_relation import RelateBatch, RelationEvidence, RelationProposal
from litgleaner.domain.reading import NewAnnotation
from tests.e2e.memory_helpers import add
from tests.e2e.relate_helpers import HERE, configure, source


def papers(harness, count=2):
    configure(harness)
    ids = tuple(add(harness, f"reading-memory-{index}") for index in range(count))
    for id_ in ids:
        source(harness, id_, HERE)
    return ids


def note(harness, paper_id, body, *, page=None, quote=None):
    return harness.ctx.store.reader.add_annotation(
        NewAnnotation(paper_id, "note", page, None, quote, body), harness.ctx.now_iso()
    )


def decide(harness, relation, action="confirm", *, kind=None):
    return apply(harness.ctx, relation.id, action, f"{action}:{relation.id}", kind=kind)


def _batch(ctx, a, ids, hashes, proposals, *, name, created, legacy):
    records = {id_: {"paper_id": id_, "fragments": [{"page": 2, "quote": HERE}]} for id_ in ids}
    return RelateBatch(
        id=name,
        paper_id=a,
        input_hash=name,
        producer_version=producer_version(ctx),
        model="fake",
        reading_language=ctx.settings.reading_language,
        token_budget=8000,
        scope_id=name,
        created_at=created,
        source_hashes=hashes,
        considered_paper_ids=ids,
        input_paper_ids=ids,
        inputs=json.dumps(
            {"pairs": [{"paper_a": records[a], "paper_b": records[b]} for b in ids if b != a]}
        ),
        relations=proposals,
        configuration={} if legacy else dict(configuration(ctx)),
    )


def relations(
    harness,
    a,
    *peers,
    name="batch",
    kind="extends",
    created="2026-01-01",
    extra=(),
    stale=False,
    legacy=False,
):
    ctx = harness.ctx
    ids = tuple(dict.fromkeys((a, *peers, *extra)))
    states = {id_: ctx.store.papers.get_fulltext(id_) for id_ in ids}
    hashes = {
        id_: source_hash(
            ctx.store.papers.get(id_), states[id_], ctx.store.papers.get_extraction(id_)
        )
        for id_ in ids
    }
    if stale:
        hashes[peers[0]] = "previous-source"
    proposals = tuple(
        RelationProposal(
            paper_a=a,
            paper_b=b,
            kind=kind,
            evidence_a=RelationEvidence(paper_id=a, doc_key=states[a].doc_key, page=2, quote=HERE),
            evidence_b=RelationEvidence(paper_id=b, doc_key=states[b].doc_key, page=2, quote=HERE),
            explanation="The original passages support this relationship.",
            source_hashes={id_: hashes[id_] for id_ in (a, b)},
        )
        for b in peers
    )
    batch = _batch(ctx, a, ids, hashes, proposals, name=name, created=created, legacy=legacy)
    ctx.store.relations.add_batch(batch)
    return tuple(r for r in ctx.store.relations.for_paper(a) if r.batch_id == name)


def one(harness, a, b, **options):
    return relations(harness, a, b, **options)[0]


def bad_blob(harness, monkeypatch, paper_id, counts=None):
    key = harness.ctx.store.papers.get_fulltext(paper_id).doc_key
    get = harness.ctx.blobs.get

    def read(requested):
        if counts is not None:
            counts[requested] += 1
        if requested == key:
            raise ValueError("private corrupt archive path")
        return get(requested)

    monkeypatch.setattr(harness.ctx.blobs, "get", read)
    return key


def block_external(harness, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("reading view must not write, infer, fetch sources or enqueue")

    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    monkeypatch.setattr(harness.ctx.embedder, "embed", forbidden)
    for route in harness.ctx.llm.routes.values():
        monkeypatch.setattr(route.llm, "complete", forbidden)
        monkeypatch.setattr(route.llm, "stream", forbidden)
    for provider in harness.ctx.sources.values():
        monkeypatch.setattr(provider, "fetch", forbidden)
