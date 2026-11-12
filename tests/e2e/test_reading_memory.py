"""阅读摘要真实批量存储、归档核验、用户决定及引用分页的离线行为。"""

from collections import Counter
from dataclasses import asdict, replace

import pytest
from sqlalchemy import delete, event

from litgleaner.application.reading_memory import view
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.identity import Alias
from litgleaner.infra.db.schema import reading_state
from tests.e2e.memory_helpers import add, citation
from tests.e2e.reading_memory_helpers import (
    bad_blob,
    block_external,
    decide,
    note,
    one,
    papers,
    relations,
)
from tests.e2e.relate_helpers import HERE, source


def _ids(result):
    return tuple(card.relation.id for card in result.relations)


def _codes(result):
    return {warning.code for warning in result.warnings}


def test_user_decision_priority_then_reverse_extension_then_time_and_three_card_limit(harness):
    a, b, c, d, e = papers(harness, 5)
    changed = decide(harness, one(harness, a, b, name="changed"), "change", kind="same_problem")
    confirmed = decide(harness, one(harness, a, c, name="confirmed", created="9999"))
    reverse = decide(harness, one(harness, d, a, name="reverse", created="0001"))
    one(harness, e, a, name="unconfirmed-reverse", created="9999")
    result = view(harness.ctx, a)
    assert _ids(result) == (reverse.id, confirmed.id, changed.id)
    assert result.relation_candidates == 4 and result.relations_checked == 3
    assert result.relations_truncated and "relations_omitted" in _codes(result)
    assert result.relations[0].direction == "incoming"
    assert result.relations[0].relation.paper_a == d
    assert result.relations[0].relation.evidence_a.paper_id == d
    assert result.relations[0].relation.evidence_b.paper_id == a


def test_model_reverse_extensions_beat_newer_outgoing_and_ties_use_relation_id(harness):
    a, b, c, d = papers(harness, 4)
    reverse = one(harness, b, a, name="reverse", created="0001")
    others = (
        one(harness, a, c, name="C", created="9999"),
        one(harness, a, d, name="D", created="9999"),
    )
    result = view(harness.ctx, a)
    assert _ids(result) == (
        reverse.id,
        *(r.id for r in sorted(others, key=lambda r: r.id, reverse=True)),
    )
    assert result.relations_checked == 3 and not result.relations_truncated
    assert result.relations[0].relation.created_at == "0001"
    assert result.relations[0].relation.evidence_a.quote == HERE


def test_sixty_four_window_uses_user_priority_before_truncation(harness):
    ids = papers(harness, 67)
    a, peers = ids[0], ids[1:]
    with harness.ctx.store.atomic():
        for index, b in enumerate(peers[:-1]):
            one(harness, a, b, name=f"stale-{index:03}", created=f"9999-{index:03}", stale=True)
        selected = decide(harness, one(harness, a, peers[-1], name="old-user", created="0001"))
    result = view(harness.ctx, a)
    assert _ids(result) == (selected.id,)
    assert result.relation_candidates == 66 and result.relations_checked == 64
    assert result.relations_truncated and "relations_omitted" in _codes(result)
    assert sum(warning.code == "relation_stale" for warning in result.warnings) == 63


def test_stale_unknown_and_rejected_relations_are_skipped_with_current_card_retained(harness):
    a, b, c, d, e = papers(harness, 5)
    current = one(harness, a, b, name="current")
    one(harness, a, c, name="stale", stale=True)
    one(harness, a, d, name="unknown", legacy=True)
    decide(harness, one(harness, a, e, name="rejected"), "reject")
    result = view(harness.ctx, a)
    assert _ids(result) == (current.id,)
    assert result.relation_candidates == result.relations_checked == 3
    assert {"relation_stale", "relation_unknown"} <= _codes(result)


def test_changed_reading_language_marks_model_relation_stale(harness):
    a, b = papers(harness)
    one(harness, a, b)
    harness.ctx = replace(
        harness.ctx, settings=replace(harness.ctx.settings, reading_language="en")
    )
    result = view(harness.ctx, a)
    assert result.relations == () and "relation_stale" in _codes(result)


def test_confirmed_relation_bypasses_model_batch_configuration_and_missing_route(harness):
    a, b, c = papers(harness, 3)
    confirmed = decide(harness, one(harness, a, b, extra=(c,), legacy=True))
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    result = view(harness.ctx, a)
    assert _ids(result) == (confirmed.id,)
    assert "relation_unknown" not in _codes(result)


def test_changed_user_relation_bypasses_model_batch_but_retains_its_original_evidence(harness):
    a, b = papers(harness)
    original = one(harness, a, b, legacy=True)
    changed = decide(harness, original, "change", kind="compares_with")
    result = view(harness.ctx, a)
    assert _ids(result) == (changed.id,)
    assert result.relations[0].relation.source == "user"
    assert result.relations[0].relation.evidence_a == original.evidence_a


def test_confirmed_relation_still_requires_current_both_side_source_fingerprints(harness):
    a, b = papers(harness)
    decide(harness, one(harness, a, b))
    source(harness, b, HERE + " changed extraction")
    result = view(harness.ctx, a)
    assert result.relations == () and "relation_stale" in _codes(result)


def test_confirmed_relation_still_requires_readable_author_document(harness, monkeypatch):
    a, b = papers(harness)
    decide(harness, one(harness, a, b))
    bad_blob(harness, monkeypatch, b)
    result = view(harness.ctx, a)
    assert result.relations == () and "relation_unknown" in _codes(result)
    assert "private" not in " ".join(warning.detail for warning in result.warnings)


def test_corrupt_third_source_makes_entire_model_batch_unknown(harness, monkeypatch):
    a, b, c = papers(harness, 3)
    relations(harness, a, b, c)
    bad_blob(harness, monkeypatch, c)
    result = view(harness.ctx, a)
    assert result.relations == ()
    assert result.relations_checked == 2
    assert sum(warning.code == "relation_unknown" for warning in result.warnings) == 2


def test_corrupt_batch_isolated_and_distinct_documents_including_failure_read_once(
    harness, monkeypatch
):
    a, b, c, d = papers(harness, 4)
    one(harness, a, b, name="broken", extra=(c,))
    current = one(harness, a, d, name="current")
    counts = Counter()
    key = bad_blob(harness, monkeypatch, c, counts)
    result = view(harness.ctx, a)
    assert _ids(result) == (current.id,)
    assert "relation_unknown" in _codes(result)
    assert counts[key] == 1 and len(counts) == 4
    assert set(counts.values()) == {1}


def test_confirmed_card_ignores_corrupt_third_batch_source(harness, monkeypatch):
    a, b, c = papers(harness, 3)
    confirmed = decide(harness, one(harness, a, b, extra=(c,)))
    bad_blob(harness, monkeypatch, c)
    result = view(harness.ctx, a)
    assert _ids(result) == (confirmed.id,)
    assert "relation_unknown" not in _codes(result)


def test_latest_user_note_excerpt_version_edit_delete_and_fallback_are_live(harness):
    a, b = papers(harness)
    one(harness, a, b)
    old = note(harness, b, "旧笔记", page=3)
    harness.clock.advance(seconds=1)
    latest = note(harness, b, "\n  " + "长" * 250 + "\n下一行", page=None)
    card = view(harness.ctx, a).relations[0]
    assert card.note.source_id == latest.id and card.note.text == "长" * 240
    assert card.note.page is None and card.note.truncated and card.note.authorship == "user"
    assert card.note.source_version == stable_hash(asdict(latest))
    harness.ctx.store.reader.update_annotation(latest.id, "现在的原话", harness.ctx.now_iso())
    edited = view(harness.ctx, a).relations[0].note
    assert edited.text == "现在的原话" and not edited.truncated
    assert edited.source_version != card.note.source_version
    harness.ctx.store.reader.delete_annotation(latest.id, harness.ctx.now_iso())
    restored = view(harness.ctx, a).relations[0].note
    assert restored.source_id == old.id and restored.text == "旧笔记" and restored.page == 3


def test_quote_without_user_body_does_not_become_user_note(harness):
    a, b = papers(harness)
    one(harness, a, b)
    note(harness, b, None, quote="作者高亮，不是用户所写")
    note(harness, b, " \n\t ")
    assert view(harness.ctx, a).relations[0].note is None


def test_bidirectional_citations_group_all_sources_and_page_by_stable_peer_id(harness):
    a, b, c = papers(harness, 3)
    first = citation(harness, a, target=b)
    second = citation(harness, a, target=b, source="other", position="source:other")
    reverse = citation(harness, b, target=a)
    citation(harness, c, target=a)
    citation(harness, a, target=a, position="self")
    outsider = add(harness, "outside", library=False)
    citation(harness, a, target=outsider, position="outside")
    harness.ctx.store.reader.set_state(b, "read", {"page": 9}, harness.ctx.now_iso())
    result = view(harness.ctx, a, limit=1)
    following = view(harness.ctx, a, limit=1, offset=1)
    assert result.total == following.total == 2 and len(result.citations) == 1
    assert tuple(row.paper.paper_id for row in (*result.citations, *following.citations)) == tuple(
        sorted((b, c))
    )
    both = next(row for row in (*result.citations, *following.citations) if row.paper.paper_id == b)
    assert set(both.cites) == {first, second} and both.cited_by == (reverse,)
    assert both.paper.reading_status == "read"
    assert view(harness.ctx, a, offset=50).citations == ()


def test_global_ambiguous_aliases_cannot_create_false_library_citation(harness):
    a, b = papers(harness)
    outside = add(harness, "outside", library=False)
    harness.ctx.store.papers.add_aliases(outside, (Alias("external", "outside"),))
    aliases = harness.ctx.store.papers.aliases_of(b)
    citation(harness, a, aliases=(*aliases, Alias("external", "outside")))
    assert view(harness.ctx, a).total == 0


def test_library_exit_removes_relation_and_citation_peer_on_next_read(harness):
    a, b = papers(harness)
    one(harness, a, b)
    citation(harness, a, target=b)
    assert view(harness.ctx, a).relations and view(harness.ctx, a).total == 1
    harness.ctx.store._db.write(delete(reading_state).where(reading_state.c.paper_id == b))
    result = view(harness.ctx, a)
    assert result.relations == () and result.total == 0
    assert "relation_outside_library" in _codes(result)


def test_without_model_routes_citations_remain_and_view_uses_only_eleven_read_queries(
    harness, monkeypatch
):
    a, b = papers(harness)
    one(harness, a, b)
    citation(harness, a, target=b)
    block_external(harness, monkeypatch)
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}, daily_budget_usd=0))
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(harness.ctx.store._db._engine, "before_cursor_execute", capture)
    try:
        result = view(harness.ctx, a)
    finally:
        event.remove(harness.ctx.store._db._engine, "before_cursor_execute", capture)
    assert result.total == 1 and result.relations == ()
    assert "relation_unknown" in _codes(result)
    assert sum(sql.startswith("SELECT") for sql in statements) == 11
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql in statements)
    assert harness.llm_calls() == 0 and not harness.ctx.store.jobs.list_by_status("pending")


@pytest.mark.parametrize("options", [{"limit": 0}, {"limit": 201}, {"limit": True}, {"offset": -1}])
def test_invalid_pagination_rejected_before_database_read(harness, monkeypatch, options):
    monkeypatch.setattr(
        harness.ctx.store, "reading_memory_snapshot", lambda _: pytest.fail("queried")
    )
    with pytest.raises(InvalidRequest):
        view(harness.ctx, "missing", **options)


def test_main_paper_must_already_belong_to_library(harness):
    outside = add(harness, "not-in-library", library=False)
    with pytest.raises(NotFound):
        view(harness.ctx, outside)
