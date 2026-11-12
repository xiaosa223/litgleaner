"""统一报告独立核验关系历史、真实文库及当前原页，始终零写入。"""

from collections import Counter
from dataclasses import replace

import pytest
from sqlalchemy import delete

from litgleaner.application import relation_feedback, relation_views
from litgleaner.application.documents import load_document, save_document
from litgleaner.application.stale import report
from litgleaner.domain.document import Block
from litgleaner.infra.db.schema import reading_state
from tests.e2e.relate_flow_helpers import change_prompt, generate, respond
from tests.e2e.relate_helpers import pair


def entries(harness):
    return {
        row.kind: row
        for row in report(harness.ctx).items
        if row.kind in ("relation", "relate_batch")
    }


def test_no_embedding_is_still_current_library_and_reads_each_doc_once(harness, monkeypatch):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    original, calls = type(harness.ctx.blobs).get, []

    def read(self, key):
        calls.append(key)
        return original(self, key)

    monkeypatch.setattr(type(harness.ctx.blobs), "get", read)
    result = entries(harness)
    assert result["relation"].status == result["relate_batch"].status == "current"
    assert len(calls) == 2 and max(Counter(calls).values()) == 1
    data = harness.ctx.store.stale_snapshot()
    assert data.library_paper_ids == {a, b} and not data.embeddings


@pytest.mark.parametrize("action", ["confirm", "reject", "change"])
def test_user_decision_survives_model_changes_but_model_batch_is_stale(
    harness, monkeypatch, action
):
    a, _ = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    old = harness.ctx.store.relations.for_paper(a)[0]
    kind = "same_problem" if action == "change" else None
    chosen = relation_feedback.apply(harness.ctx, old.id, action, "decided", kind=kind)
    change_prompt(harness)
    view = relation_views.view(harness.ctx, a)
    selected = {row.id: row for row in view.checks}
    assert selected[chosen.id].status == "current"
    assert selected[chosen.id].selected == (action != "reject")
    assert any(row.kind == "relate_batch" and row.status == "stale" for row in view.checks)
    if action == "change":
        assert selected[old.id].status == "stale" and selected[old.id].selected is False


def test_confirmed_current_does_not_exempt_unconfirmed_old_model_history(harness, monkeypatch):
    a, _ = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    old = harness.ctx.store.relations.for_paper(a)[0]
    change_prompt(harness)
    generate(harness, a)
    current = next(row for row in harness.ctx.store.relations.for_paper(a) if row.status == "valid")
    relation_feedback.apply(harness.ctx, current.id, "confirm", "confirm-new")
    change_prompt(harness)
    rows = {row.id: row for row in relation_views.view(harness.ctx, a).checks}
    assert rows[current.id].status == "current" and rows[old.id].status == "stale"


def test_same_pointer_changed_original_page_marks_relation_and_batch_stale(harness, monkeypatch):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    key = harness.ctx.store.papers.get_fulltext(b).doc_key
    doc = load_document(harness.ctx, key)
    actual, _ = save_document(
        harness.ctx,
        replace(doc, blocks=(Block("text", "A wholly different original passage.", 2),)),
    )
    assert actual == key
    result = entries(harness)
    assert result["relation"].status == result["relate_batch"].status == "stale"
    assert any("指定页" in text for text in result["relate_batch"].reasons)


def test_missing_archive_is_unknown_without_hiding_other_records(harness, monkeypatch):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    original = type(harness.ctx.blobs).get
    broken = harness.ctx.store.papers.get_fulltext(b).doc_key

    def read(self, key):
        if key == broken:
            raise ValueError("private filesystem path")
        return original(self, key)

    monkeypatch.setattr(type(harness.ctx.blobs), "get", read)
    result = entries(harness)
    assert result["relation"].status == result["relate_batch"].status == "unknown"
    assert all("private" not in text for row in result.values() for text in row.unchecked)


def test_missing_routes_are_unknown_while_known_source_mismatch_remains_stale(harness, monkeypatch):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    harness.ctx = replace(
        harness.ctx,
        llm=replace(
            harness.ctx.llm,
            routes={
                name: route
                for name, route in harness.ctx.llm.routes.items()
                if name not in ("relate", "extract")
            },
        ),
    )
    assert {row.status for row in entries(harness).values()} == {"unknown"}
    harness.ctx.store._db.write(delete(reading_state).where(reading_state.c.paper_id == b))
    result = entries(harness)
    assert {row.status for row in result.values()} == {"stale"}


def test_frozen_snapshot_needs_no_extra_queries_or_writes(harness, monkeypatch):
    a, _ = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    data = harness.ctx.store.stale_snapshot()

    def forbidden(*args, **kwargs):
        raise AssertionError("report escaped its frozen database snapshot")

    monkeypatch.setattr(harness.ctx.store, "stale_snapshot", lambda: data)
    monkeypatch.setattr(harness.ctx.store._db, "fetch", forbidden)
    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    monkeypatch.setattr(type(harness.llm), "complete", forbidden)
    assert {row.status for row in entries(harness).values()} == {"current"}
