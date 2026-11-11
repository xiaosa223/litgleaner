"""日常被引发现保存准确来源事实，保持游标、事务和零额外费用合同。"""

from dataclasses import replace

import pytest
from sqlalchemy import select

from litgleaner.adapters.sources.semantic_scholar import SemanticScholarSource
from litgleaner.application.codec import decompress
from litgleaner.application.stages.discover import DiscoverStage, resolve_record
from litgleaner.domain.citation import SourceCitation
from litgleaner.domain.identity import Alias, record_aliases
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import CitationWatch, KeywordSearch
from litgleaner.infra.db.schema import (
    citation,
    merge_log,
    paper,
    paper_alias,
    sighting,
    source_cursor,
)
from litgleaner.ports.source import FetchResult
from litgleaner.ports.store import TriageResult
from tests.e2e.conftest import Harness
from tests.helpers.http import Router


class DailySource:
    capabilities = frozenset({CitationWatch, KeywordSearch})

    def __init__(self, result: FetchResult, name: str = "daily") -> None:
        self.name, self.result = name, result
        self.calls = []

    def fetch(self, query, cursor):
        self.calls.append((query, cursor))
        return self.result


def record(name, *, doi=True, **changes):
    return SourceRecord(title=f"Paper {name}", doi=f"10.1000/{name}" if doi else None, **changes)


def saved(harness, item):
    return resolve_record(harness.ctx.store.papers, item, harness.ctx.now_iso())


def found_id(harness, item):
    ids = harness.ctx.store.papers.find_ids_by_aliases(record_aliases(item))
    assert len(ids) == 1
    return next(iter(ids))


def edge(citing, target, *, position=0, **changes):
    return SourceCitation(
        record_aliases(citing),
        (Alias("doi", f"10.1000/{target}"),),
        position,
        f"Source evidence: {citing.title} cites {target}",
        **changes,
    )


def run(harness, result, *, query=None, name="daily"):
    source = DailySource(result, name)
    ctx = replace(harness.ctx, sources={name: source})
    query = query or CitationWatch(dois=("10.1000/x", "10.1000/y"))
    DiscoverStage().process(ctx, {"source": name, "query": query.model_dump(mode="json")})
    return source


def facts(harness):
    db = harness.ctx.store._db
    return tuple(
        tuple(db.fetch(select(table)))
        for table in (paper, paper_alias, sighting, source_cursor, citation, merge_log)
    )


def test_daily_multi_seed_edges_keep_exact_direction_and_source_evidence(harness: Harness):
    x, y = saved(harness, record("x")), saved(harness, record("y"))
    a, b, c = (record(name) for name in ("a", "b", "c"))
    found = FetchResult(
        (a, b, c),
        "2026-11-03",
        citations=(
            edge(a, "x", contexts=("Uses X.",), intents=("methodology",), influential=True),
            edge(b, "y", position=1),
            edge(c, "x", position=2),
            edge(c, "y", position=3),
        ),
    )
    source = run(harness, found)
    ids = {item.title: found_id(harness, item) for item in (a, b, c)}
    citations = harness.ctx.store.citations
    assert {item.cited_paper_id for item in citations.outgoing(ids[a.title])} == {x}
    assert {item.cited_paper_id for item in citations.outgoing(ids[b.title])} == {y}
    assert {item.cited_paper_id for item in citations.outgoing(ids[c.title])} == {x, y}
    (first,) = citations.outgoing(ids[a.title])
    assert first.source == "daily" and first.raw_text == found.citations[0].raw_text
    assert first.contexts == ("Uses X.",) and first.intents == ("methodology",)
    assert first.influential is True and first.page is None and first.bbox is None
    assert len(source.calls) == 1 and harness.llm_calls() == 0


def test_replays_query_and_position_reordering_keep_facts_but_changed_evidence_appends(harness):
    saved(harness, record("x"))
    a, b = record("a"), record("b")
    items = (edge(a, "x"), edge(b, "x", position=1))
    run(harness, FetchResult((a, b), "first", citations=items))
    paper_id = found_id(harness, a)
    before = harness.ctx.store.citations.outgoing(paper_id)
    harness.clock.advance(days=1)
    reordered = tuple(
        replace(item, source_position=100 + i) for i, item in enumerate(reversed(items))
    )
    run(
        harness,
        FetchResult((b, a), "second", citations=reordered),
        query=CitationWatch(dois=("10.1000/y", "10.1000/x")),
    )
    assert harness.ctx.store.citations.outgoing(paper_id) == before
    updated = replace(
        items[0], raw_text="Source revised its evidence", contexts=("Different context",)
    )
    run(harness, FetchResult((a,), "third", citations=(updated,)))
    after = harness.ctx.store.citations.outgoing(paper_id)
    assert len(after) == 2 and before[0] in after
    assert {item.raw_text for item in after} == {items[0].raw_text, updated.raw_text}


def test_external_id_only_citing_paper_is_linked_exactly(harness):
    target = saved(harness, record("x"))
    citing = record("without-doi", doi=False, external_ids={"catalog": "external-123"})
    run(harness, FetchResult((citing,), "next", citations=(edge(citing, "x"),)))
    outgoing = harness.ctx.store.citations.outgoing(found_id(harness, citing))
    assert len(outgoing) == 1 and outgoing[0].cited_paper_id == target


@pytest.mark.parametrize("bad_id", [True, ["bad"], {"id": "bad"}, "   "])
def test_valid_dois_with_malformed_source_ids_keep_distinct_citing_papers(harness, bad_id):
    targets = {name: saved(harness, record(name)) for name in ("x", "y")}
    router = Router()
    for target, name in [("x", "a"), ("y", "b")]:
        router.reply(
            f"DOI:10.1000/{target}/citations",
            json_body={
                "data": [
                    {
                        "citingPaper": {
                            "paperId": bad_id,
                            "externalIds": {"DOI": f"10.1000/{name}"},
                            "title": f"Paper {name}",
                            "publicationDate": harness.ctx.clock().date().isoformat(),
                        }
                    }
                ]
            },
        )
    source = SemanticScholarSource(
        "daily",
        {"min_interval_seconds": 0},
        contact_email=None,
        transport=router.transport,
        today=lambda: harness.ctx.clock().date(),
    )
    ctx = replace(harness.ctx, sources={"daily": source})
    query = CitationWatch(dois=("10.1000/x", "10.1000/y"))
    DiscoverStage().process(ctx, {"source": "daily", "query": query.model_dump(mode="json")})
    a, b = (found_id(harness, record(name)) for name in ("a", "b"))
    assert a != b
    assert {e.cited_paper_id for e in ctx.store.citations.outgoing(a)} == {targets["x"]}
    assert {e.cited_paper_id for e in ctx.store.citations.outgoing(b)} == {targets["y"]}
    assert len(router.requests) == 2 and harness.llm_calls() == 0


def test_conflicting_citing_identifiers_never_attach_to_first_resolution_candidate(harness):
    saved(harness, record("x"))
    first = record("first", external_ids={"catalog": "first"})
    second = record("second", external_ids={"catalog": "second"})
    ids = (saved(harness, first), saved(harness, second))
    conflict = first.model_copy(update={"external_ids": {"catalog": "second"}})
    run(harness, FetchResult((conflict,), "next", citations=(edge(conflict, "x"),)))
    assert all(harness.ctx.store.citations.outgoing(paper_id) == () for paper_id in ids)
    assert harness.ctx.store._db.fetch(select(merge_log.c.status))[-1][0] == "pending"


def test_unknown_target_aliases_survive_and_become_incoming_when_target_arrives(harness):
    citing = record("a")
    run(harness, FetchResult((citing,), "next", citations=(edge(citing, "x"),)))
    outgoing = harness.ctx.store.citations.outgoing(found_id(harness, citing))
    assert len(outgoing) == 1 and outgoing[0].cited_paper_id is None
    assert outgoing[0].cited_aliases == (Alias("doi", "10.1000/x"),)
    target = saved(harness, record("x"))
    assert harness.ctx.store.citations.incoming(target) == outgoing
    assert harness.ctx.store.citations.outgoing(found_id(harness, citing)) == outgoing


def test_edge_for_old_citing_paper_absent_from_returned_records_is_ignored(harness):
    saved(harness, record("x"))
    old = record("old-citing")
    old_id = saved(harness, old)
    returned = record("returned")
    run(harness, FetchResult((returned,), "next", citations=(edge(old, "x"),)))
    assert harness.ctx.store.citations.outgoing(old_id) == ()
    assert harness.ctx.store.citations.outgoing(found_id(harness, returned)) == ()


def test_non_citation_query_never_ingests_supplied_source_edges(harness):
    saved(harness, record("x"))
    citing = record("a")
    run(
        harness,
        FetchResult((citing,), "next", citations=(edge(citing, "x"),)),
        query=KeywordSearch(terms=("alpha",)),
    )
    assert harness.ctx.store.citations.outgoing(found_id(harness, citing)) == ()


def test_truncation_ingests_only_returned_citing_papers_and_keeps_cursor(harness):
    target = saved(harness, record("x"))
    accepted, omitted = record("accepted"), record("omitted")
    omitted_id = saved(harness, omitted)
    query = CitationWatch(dois=("10.1000/x",))
    harness.ctx.store.papers.set_cursor("daily", query.hash, "old-window", harness.ctx.now_iso())
    result = FetchResult(
        (accepted,),
        "new-window",
        truncated=True,
        citations=(edge(accepted, "x"), edge(omitted, "x")),
    )
    source = run(harness, result, query=query)
    outgoing = harness.ctx.store.citations.outgoing(found_id(harness, accepted))
    assert len(outgoing) == 1 and outgoing[0].cited_paper_id == target
    assert harness.ctx.store.citations.outgoing(omitted_id) == ()
    assert source.calls == [(query, "old-window")]
    assert harness.ctx.store.papers.get_cursor("daily", query.hash) == "old-window"


def test_citation_write_failure_rolls_back_entire_discovery_batch(harness, monkeypatch):
    saved(harness, record("x"))
    a, b = record("a"), record("b")
    before = facts(harness)
    original = harness.ctx.store.citations.add
    writes = []

    def failing(item):
        writes.append(item)
        if len(writes) == 2:
            raise RuntimeError("citation storage unavailable")
        return original(item)

    monkeypatch.setattr(harness.ctx.store.citations, "add", failing)
    with pytest.raises(RuntimeError, match="citation storage unavailable"):
        run(harness, FetchResult((a, b), "next", citations=(edge(a, "x"), edge(b, "x"))))
    assert len(writes) == 2 and facts(harness) == before


def test_raw_archive_failure_prevents_all_database_changes(harness, monkeypatch):
    saved(harness, record("x"))
    citing = record("a")
    before = facts(harness)
    source = DailySource(
        FetchResult((citing,), "next", raw=b"original raw evidence", citations=(edge(citing, "x"),))
    )
    ctx = replace(harness.ctx, sources={source.name: source})

    def failing(*_args):
        raise OSError("archive unavailable")

    monkeypatch.setattr(ctx.blobs, "put", failing)
    query = CitationWatch(dois=("10.1000/x",))
    with pytest.raises(OSError, match="archive unavailable"):
        DiscoverStage().process(
            ctx, {"source": source.name, "query": query.model_dump(mode="json")}
        )
    assert facts(harness) == before and len(source.calls) == 1 and harness.llm_calls() == 0


def test_discovery_does_not_call_models_or_fetch_again_for_edges(harness, monkeypatch):
    saved(harness, record("x"))
    citing = record("a")

    def forbidden(*_args, **_kwargs):
        pytest.fail("引用关系摄取不得调用模型")

    monkeypatch.setattr(harness.llm, "complete", forbidden)
    monkeypatch.setattr(harness.llm, "stream", forbidden)
    source = run(harness, FetchResult((citing,), "next", citations=(edge(citing, "x"),)))
    assert len(source.calls) == 1 and harness.llm_calls() == 0
    assert len(harness.ctx.store.citations.outgoing(found_id(harness, citing))) == 1


def test_reading_route_counts_distinct_citing_papers_across_sources(harness):
    target = saved(harness, record("x"))
    citing = record("a")
    result = FetchResult((citing,), "next", citations=(edge(citing, "x"),))
    run(harness, result, name="first")
    run(harness, result, name="second")
    paper_id = found_id(harness, citing)
    topic = harness.ctx.topics["alpha-topic"]
    rubric = harness.ctx.rubrics[topic.rubric]
    for candidate in (target, paper_id):
        harness.ctx.store.judgments.save_triage(
            TriageResult(candidate, topic.id, topic.topic_hash, True, "Relevant", "v", "rule"),
            harness.ctx.now_iso(),
        )
    snapshot = harness.ctx.store.reading_routes.snapshot(
        topic.id, topic.topic_hash, rubric.semantic_hash, rubric.policy_hash
    )
    assert len(harness.ctx.store.citations.outgoing(paper_id)) == 2
    assert snapshot.citations == {target: 1}


def test_alias_order_and_duplicate_aliases_do_not_create_new_evidence_versions(harness):
    target_record = record("x", external_ids={"catalog": "target"})
    target = saved(harness, target_record)
    citing = record("a", external_ids={"catalog": "citing"})
    item = replace(edge(citing, "x"), cited_aliases=record_aliases(target_record))
    run(harness, FetchResult((citing,), "first", citations=(item,)))
    paper_id = found_id(harness, citing)
    before = harness.ctx.store.citations.outgoing(paper_id)
    changed_order = replace(
        item,
        citing_aliases=(*reversed(item.citing_aliases), item.citing_aliases[0]),
        cited_aliases=(*reversed(item.cited_aliases), item.cited_aliases[0]),
        source_position=90,
    )
    run(harness, FetchResult((citing,), "second", citations=(changed_order,)))
    assert len(before) == 1 and before[0].cited_paper_id == target
    assert harness.ctx.store.citations.outgoing(paper_id) == before


def test_conflicting_target_aliases_keep_raw_fact_without_false_incoming_link(harness):
    x = saved(harness, record("x"))
    y = saved(harness, record("y", external_ids={"catalog": "different-target"}))
    citing = record("a")
    aliases = (Alias("doi", "10.1000/x"), Alias("catalog", "different-target"))
    item = replace(edge(citing, "x"), cited_aliases=aliases)
    run(harness, FetchResult((citing,), "next", citations=(item,)))
    outgoing = harness.ctx.store.citations.outgoing(found_id(harness, citing))
    assert len(outgoing) == 1 and outgoing[0].cited_paper_id is None
    assert outgoing[0].cited_aliases == aliases and outgoing[0].raw_text == item.raw_text
    assert harness.ctx.store.citations.incoming(x) == harness.ctx.store.citations.incoming(y) == ()


@pytest.mark.parametrize("missing", ["citing_aliases", "cited_aliases"])
def test_edge_without_exact_endpoint_identifiers_is_ignored(harness, missing):
    saved(harness, record("x"))
    citing = record("a")
    item = replace(edge(citing, "x"), **{missing: ()})
    run(harness, FetchResult((citing,), "next", citations=(item,)))
    assert harness.ctx.store.citations.outgoing(found_id(harness, citing)) == ()


def test_raw_response_remains_archived_and_sighting_points_to_original_evidence(harness):
    saved(harness, record("x"))
    citing = record("a")
    raw = b'{"data":[{"source":"original citation evidence"}]}'
    query = CitationWatch(dois=("10.1000/x",))
    result = FetchResult((citing,), "complete-window", raw=raw, citations=(edge(citing, "x"),))
    source = run(harness, result, query=query)
    rows = harness.ctx.store._db.fetch(select(sighting).where(sighting.c.source == "daily"))
    assert len(rows) == 1 and rows[0].paper_id == found_id(harness, citing)
    assert rows[0].query_hash == query.hash and rows[0].raw_key
    assert decompress(harness.ctx.blobs.get(rows[0].raw_key)) == raw
    assert harness.ctx.store.papers.get_cursor("daily", query.hash) == "complete-window"
    assert len(source.calls) == 1
