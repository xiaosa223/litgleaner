"""全局引文身份通过事实、候选、重要前作及阅读摘要保持同一证据归属。"""

from dataclasses import replace

import pytest
from sqlalchemy import event

from litgleaner.application import memory, memory_candidates, reading_memory, relate
from litgleaner.application.pipeline import run_on_demand
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.identity import Alias
from litgleaner.domain.memory_policy import MemoryPolicy
from tests.e2e.memory_helpers import add, citation
from tests.e2e.relate_helpers import HERE, THERE, configure, source

X, Y = Alias("external", "unresolved-x"), Alias("external", "unresolved-y")


def _setup(harness):
    weights = dict.fromkeys(MemoryPolicy().weights.model_dump(), 0.0)
    weights.update(direct_citation=6.0, citation_intent=5.0)
    policy = MemoryPolicy(weights=weights)
    harness.ctx = replace(
        harness.ctx,
        embedder=None,
        settings=replace(harness.ctx.settings, memory_policy=policy),
    )
    a, b, bridge, other = (add(harness, name) for name in ("anchor", "prior", "bridge", "other"))
    outside = add(harness, "outside", library=False)
    return a, b, bridge, other, outside


def _chain(harness, ids, *, ambiguous, reverse=False):
    a, b, bridge, other, outside = ids
    owned = harness.ctx.store.papers.aliases_of(b)
    outer = harness.ctx.store.papers.aliases_of(outside)
    edges = (
        ((a, (*owned, X), "anchor"), (bridge, (X, Y), "bridge"), (other, (Y, *outer), "other"))
        if ambiguous
        else ((a, (X,), "anchor"), (a, (X,), "second-source"), (bridge, (X, *owned), "bridge"))
    )
    result = {}
    for citing, aliases, position in reversed(edges) if reverse else edges:
        result[position] = citation(
            harness,
            citing,
            aliases=aliases,
            position=position,
            source="remote" if position == "second-source" else "parsed",
        )
    return result


def _fact(result, target):
    return next((row for row in result.matches if row.paper.paper_id == target), None)


def _link(result, target):
    return next((row for row in result.citations if row.paper.paper_id == target), None)


@pytest.mark.parametrize("reverse", [False, True])
def test_global_ambiguous_chain_never_creates_direct_facts_or_candidate_intent_weight(
    harness, reverse
):
    ids = _setup(harness)
    a, b, *_ = ids
    _chain(harness, ids, ambiguous=True, reverse=reverse)
    facts = memory.facts(harness.ctx, a)
    ranked = memory_candidates.candidates(harness.ctx, a)
    reading = reading_memory.view(harness.ctx, a)
    assert _fact(facts, b) is None
    assert facts.matches == ()
    assert ranked.matches == () and ranked.total == 0
    assert reading.citations == () and reading.total == 0
    assert memory.important_works(harness.ctx).works == ()


@pytest.mark.parametrize("reverse", [False, True])
def test_unambiguous_transitive_identity_resolves_every_original_source(harness, reverse):
    ids = _setup(harness)
    a, b, bridge, *_ = ids
    records = _chain(harness, ids, ambiguous=False, reverse=reverse)
    facts = memory.facts(harness.ctx, a)
    original = {records["anchor"], records["second-source"]}
    assert set(_fact(facts, b).cites) == original
    assert set(_fact(memory.facts(harness.ctx, b), a).cited_by) == original
    ranked = memory_candidates.candidates(harness.ctx, a)
    assert len(ranked.matches) == 1
    assert ranked.matches[0].facts.paper.paper_id == b
    assert ranked.matches[0].signals.direct_citation and ranked.matches[0].signals.citation_intent
    assert ranked.matches[0].score.total == 11
    reading = reading_memory.view(harness.ctx, a)
    assert set(_link(reading, b).cites) == original and reading.total == 1
    (work,) = memory.important_works(harness.ctx).works
    assert work.target.paper_id == b and work.citing_count == 2
    supports = {support.paper.paper_id: support.citations for support in work.citing_papers}
    assert set(supports[a]) == original and supports[bridge] == (records["bridge"],)


@pytest.mark.parametrize("reverse", [False, True])
def test_explicit_fact_survives_ambiguous_alias_component_without_dynamic_evidence_leak(
    harness, reverse
):
    ids = _setup(harness)
    a, b, bridge, *_ = ids
    _chain(harness, ids, ambiguous=True, reverse=reverse)
    explicit = citation(harness, a, target=b, position="explicit")
    second = citation(harness, bridge, target=b, position="explicit-bridge")
    facts = memory.facts(harness.ctx, a)
    assert _fact(facts, b).cites == (explicit,)
    ranked = memory_candidates.candidates(harness.ctx, a)
    assert ranked.matches[0].facts.cites == (explicit,)
    assert ranked.matches[0].score.total == 11
    reading = reading_memory.view(harness.ctx, a)
    assert _link(reading, b).cites == (explicit,)
    (work,) = memory.important_works(harness.ctx).works
    assert work.target.paper_id == b and work.citing_count == 2
    assert {item for support in work.citing_papers for item in support.citations} == {
        explicit,
        second,
    }


@pytest.mark.parametrize(
    "entry,expected",
    [("facts", 8), ("candidates", 10), ("important", 8), ("reading", 11)],
)
def test_shared_identity_projection_preserves_fixed_read_only_query_contract(
    harness, monkeypatch, entry, expected
):
    ids = _setup(harness)
    a, b, *_ = ids
    _chain(harness, ids, ambiguous=False)
    before = harness.ctx.store.stats()

    def forbidden(*_args, **_kwargs):
        pytest.fail("identity projection must not write, call models, fetch or enqueue")

    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    for route in harness.ctx.llm.routes.values():
        monkeypatch.setattr(route.llm, "complete", forbidden)
        monkeypatch.setattr(route.llm, "stream", forbidden)
    for provider in harness.ctx.sources.values():
        monkeypatch.setattr(provider, "fetch", forbidden)
    calls = {
        "facts": lambda: memory.facts(harness.ctx, a),
        "candidates": lambda: memory_candidates.candidates(harness.ctx, a),
        "important": lambda: memory.important_works(harness.ctx),
        "reading": lambda: reading_memory.view(harness.ctx, a),
    }
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(harness.ctx.store._db._engine, "before_cursor_execute", capture)
    try:
        result = calls[entry]()
    finally:
        event.remove(harness.ctx.store._db._engine, "before_cursor_execute", capture)
    assert result is not None and b != a
    assert sum(sql.startswith("SELECT") for sql in statements) == expected
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql in statements)
    assert harness.ctx.store.stats() == before and harness.llm_calls() == 0
    assert not harness.ctx.store.jobs.list_by_status("pending")


def _source_identity(harness, ids):
    repo = harness.ctx.store.papers
    return tuple((repo.get(id_), repo.get_fulltext(id_), repo.get_extraction(id_)) for id_ in ids)


def _extend_ambiguity(harness, ids):
    _, _, bridge, other, outside = ids
    citation(harness, bridge, aliases=(X, Y), position="new-bridge")
    citation(
        harness,
        other,
        aliases=(Y, *harness.ctx.store.papers.aliases_of(outside)),
        position="new-outside",
    )


@pytest.mark.parametrize("changed_after", ["preview", "queue"])
def test_new_alias_ambiguity_stops_frozen_relation_work_before_any_model_call(
    harness, monkeypatch, changed_after
):
    ids = _setup(harness)
    a, b, *_ = ids
    configure(harness)
    source(harness, a, HERE)
    source(harness, b, THERE)
    _chain(harness, ids, ambiguous=False)
    original = _source_identity(harness, (a, b))
    policy = harness.ctx.settings.memory_policy.policy_hash
    plan = relate.preview(harness.ctx, a)
    assert plan.pairs_count == 1 and set(plan.input_paper_ids) == {a, b}
    queued = relate.queue(harness.ctx, a, plan.plan_hash) if changed_after == "queue" else None

    def forbidden(*_args, **_kwargs):
        pytest.fail("changed citation identity must stop before any model call")

    monkeypatch.setattr(harness.llm, "complete", forbidden)
    _extend_ambiguity(harness, ids)
    assert _source_identity(harness, (a, b)) == original
    assert harness.ctx.settings.memory_policy.policy_hash == policy
    current = relate.preview(harness.ctx, a)
    assert current.plan_hash != plan.plan_hash and current.pairs_count == 0
    if queued is None:
        with pytest.raises(InvalidRequest, match="重新预览"):
            relate.queue(harness.ctx, a, plan.plan_hash)
        assert not harness.ctx.store.jobs.list_by_status("pending")
    else:
        run_on_demand(harness.ctx)
        result = relate.status(harness.ctx, a, queued.job_key)
        assert result.status == "dead" and result.result is None
        assert "重新预览" in result.last_error
    assert harness.llm_calls() == 0
    assert harness.ctx.store.relations.batches_for_paper(a) == ()
