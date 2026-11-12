"""核实完成凭据、关系历史与用户决定在同一事务中保持一致。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from tests.integration.test_export_repo import paper

from litgleaner.domain.paper_relation import RelateBatch, RelationEvidence, RelationProposal
from litgleaner.infra.db import stale_repo
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import memory_admission, paper_relation, reading_state, relate_batch
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import FeedbackRecord


def proposal(a="A", b="B", kind="extends"):
    return RelationProposal(
        paper_a=a,
        paper_b=b,
        kind=kind,
        evidence_a=RelationEvidence(
            paper_id=a, doc_key=f"doc-{a}", page=0, quote=f"Original evidence from {a}."
        ),
        evidence_b=RelationEvidence(
            paper_id=b, doc_key=f"doc-{b}", page=1, quote=f"Original evidence from {b}."
        ),
        explanation="Verified relationship",
        source_hashes={a: f"source-{a}", b: f"source-{b}"},
    )


def batch(id_="batch", *, relations=None, **changes):
    values = {
        "id": id_,
        "paper_id": "A",
        "input_hash": id_,
        "producer_version": "producer",
        "model": "fake",
        "reading_language": "zh-CN",
        "token_budget": 8000,
        "scope_id": "scope",
        "created_at": "t",
        "source_hashes": {"A": "source-A", "B": "source-B"},
        "considered_paper_ids": ("A", "B"),
        "input_paper_ids": ("A", "B"),
        "inputs": "frozen complete input",
        "relations": (proposal(),) if relations is None else relations,
        "warnings": ("retained warning",),
        "configuration": {"policy": "frozen"},
    }
    return RelateBatch(**(values | changes))


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'relations.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    for id_ in ("A", "B", "C"):
        store.papers.insert(paper(id_))
        store.reader.set_state(id_, "read", None, "t")
    yield store
    store.dispose()


def decide(store, relation, action, kind=None, *, feedback_id="feedback"):
    record = FeedbackRecord(
        feedback_id,
        "A",
        "reader",
        action,
        external_id=feedback_id,
        relation_id=relation.id,
        payload={"kind": kind},
    )
    with store.atomic():
        store.feedback.add(record, "t2")
        return store.relations.apply_feedback(relation.id, action, feedback_id, kind, "t2")


def test_admission_is_first_unique_event_and_order_survives_reentry(store):
    first = store.admissions.add("B", {"route": None}, "same-second")
    second = store.admissions.add("A", {"route": "configured"}, "same-second")
    assert first.id < second.id and not first.baseline
    assert store.admissions.before(first.id) == ()
    assert store.admissions.before(second.id) == ("B",)
    assert store.admissions.add("B", {"changed": True}, "later") == first
    assert store.admissions.get("B") == first
    assert store.admissions.get("missing") is None


@pytest.mark.parametrize(
    "statement", [update(memory_admission).values(configuration="{}"), delete(memory_admission)]
)
def test_admission_ledger_cannot_be_rewritten_or_deleted(store, statement):
    original = store.admissions.add("A", {"model": None}, "t")
    with pytest.raises(IntegrityError, match="memory_admission_immutable"):
        store._db.write(statement)
    assert store.admissions.get("A") == original


def test_fingerprint_keeps_complete_first_result_and_empty_batch_is_a_cache_hit(store):
    original = store.relations.add_batch(batch(relations=()))
    replay = batch(
        "other", input_hash=original.input_hash, inputs="changed", relations=(proposal(),)
    )
    assert store.relations.add_batch(replay) == original
    assert store.relations.for_paper("A") == ()
    assert store.relations.by_fingerprint("A", original.input_hash, "producer") == original
    assert store.relations.batch("other") is None
    assert store.relations.batches_for_paper("A") == (original,)


def test_verified_new_model_judgment_supersedes_unordered_pair_and_empty_does_not(store):
    first = store.relations.add_batch(batch("first"))
    old = store.relations.for_paper("A")[0]
    second = store.relations.add_batch(
        batch("second", relations=(proposal("B", "A", "compares_with"),), created_at="t2")
    )
    current, history = store.relations.for_paper("A")
    assert current.paper_a == "B" and current.paper_b == "A"
    assert current.status == "valid" and history.status == "superseded"
    assert history.id == old.id and history.superseded_by == current.id
    assert store.relations.for_paper("B") == (current, history)
    store.relations.add_batch(batch("unrelated", relations=(), created_at="t3"))
    assert store.relations.get(current.id) == current
    assert store.relations.batch(first.id) == first and store.relations.batch(second.id) == second


def test_symmetric_relation_sorts_identifiers_and_swaps_both_quotes(store):
    saved = store.relations.add_batch(batch(relations=(proposal("B", "A", "contradicts"),)))
    relation = store.relations.for_paper("A")[0]
    assert relation.paper_a == "A" and relation.paper_b == "B"
    assert relation.evidence_a.paper_id == "A" and relation.evidence_a.doc_key == "doc-A"
    assert relation.evidence_b.paper_id == "B" and relation.evidence_b.page == 0
    assert saved.relations[0].evidence_a == relation.evidence_a


@pytest.mark.parametrize("action", ["confirm", "reject", "change"])
def test_user_decisions_prevent_model_overwrite_and_preserve_original_batch_output(store, action):
    store.relations.add_batch(batch())
    original = store.relations.for_paper("A")[0]
    decided = decide(store, original, action, "same_problem" if action == "change" else None)
    before = store.relations.for_paper("A")
    assert store.relations.user_decided_pairs() == frozenset({("A", "B")})
    later = store.relations.add_batch(batch("later", relations=(proposal(kind="contradicts"),)))
    assert later.relations and store.relations.for_paper("A") == before
    assert store.relations.get(decided.id) == decided
    if action == "change":
        assert decided.source == "user" and decided.feedback_id == "feedback"
        assert decided.evidence_a == original.evidence_a
        assert store.relations.get(original.id).superseded_by == decided.id


def test_reject_can_be_explicitly_restored_and_old_history_can_replace_current(store):
    store.relations.add_batch(batch())
    old = store.relations.for_paper("A")[0]
    store.relations.add_batch(batch("new", created_at="t1"))
    current = store.relations.for_paper("A")[0]
    rejected = decide(store, old, "reject", feedback_id="reject")
    assert rejected.status == "rejected_by_user" and rejected.superseded_by is None
    restored = decide(store, old, "confirm", feedback_id="restore")
    assert restored.status == "valid" and restored.superseded_by is None
    assert store.relations.get(current.id).superseded_by == old.id


def test_relation_feedback_does_not_hide_card_save_or_dismiss(store):
    assert store.feedback.add(FeedbackRecord("a", "A", "reader", "save_to_library"), "t")
    store.relations.add_batch(batch())
    relation = store.relations.for_paper("A")[0]
    decide(store, relation, "confirm", feedback_id="z")
    assert store.feedback.pending_saves("library") == ("A",)
    assert store.feedback.latest_action("A") == "save_to_library"
    loaded = store.feedback.by_external_id("z")
    assert loaded.relation_id == relation.id and loaded.payload == {"kind": None}
    assert not store.feedback.add(replace(loaded, id="replay"), "later")
    store.feedback.add(FeedbackRecord("zz", "A", "reader", "dismiss"), "t3")
    decide(store, relation, "reject", feedback_id="zzz")
    assert (
        store.feedback.latest_action("A") == "dismiss"
        and store.feedback.pending_saves("library") == ()
    )


def test_relation_content_is_frozen_and_self_supersession_or_wrong_pair_is_blocked(store):
    store.relations.add_batch(batch())
    relation = store.relations.for_paper("A")[0]
    for changes in (
        {"data": "{}"},
        {"kind": "contradicts"},
        {"paper_b": "C"},
        {"feedback_id": "other"},
    ):
        with pytest.raises(IntegrityError, match="paper_relation_immutable"):
            store._db.write(update(paper_relation).values(**changes))
    with pytest.raises(IntegrityError):
        store._db.write(
            update(paper_relation).values(status="superseded", superseded_by=relation.id)
        )
    with pytest.raises(IntegrityError, match="paper_relation_immutable"):
        store._db.write(delete(paper_relation))
    assert store.relations.get(relation.id) == relation


def test_replace_cannot_overwrite_batch_relation_or_admission(store):
    store.admissions.add("A", {}, "t")
    store.relations.add_batch(batch())
    for table in (memory_admission, relate_batch, paper_relation):
        row = store._db.fetch(select(table))[0]
        with pytest.raises(IntegrityError, match="immutable"):
            store._db.write(insert(table).prefix_with("OR REPLACE").values(**dict(row._mapping)))


def test_failed_second_relation_rolls_back_batch_first_relation_and_prior_supersession(
    store, monkeypatch
):
    store.relations.add_batch(batch("original"))
    old = store.relations.for_paper("A")
    original_insert = store.relations._insert

    def fail(record):
        if record.paper_b == "C":
            raise RuntimeError("failed second relation")
        original_insert(record)

    monkeypatch.setattr(store.relations, "_insert", fail)
    with pytest.raises(RuntimeError):
        store.relations.add_batch(
            batch(
                "failed",
                relations=(proposal(), proposal("A", "C")),
                source_hashes={"A": "source-A", "B": "source-B", "C": "source-C"},
                considered_paper_ids=("A", "B", "C"),
                input_paper_ids=("A", "B", "C"),
            )
        )
    assert store.relations.batch("failed") is None
    assert store.relations.for_paper("A") == old


def test_outer_feedback_failure_rolls_back_feedback_and_relation_together(store):
    store.relations.add_batch(batch())
    original = store.relations.for_paper("A")[0]
    with pytest.raises(RuntimeError), store.atomic():
        decide(store, original, "reject")
        raise RuntimeError("outer operation failed")
    assert store.feedback.by_external_id("feedback") is None
    assert store.relations.get(original.id) == original


def test_invalid_model_copy_or_nonlibrary_endpoint_cannot_create_batch(store):
    with pytest.raises(ValidationError):
        store.relations.add_batch(batch().model_copy(update={"source_hashes": {}}))
    store._db.write(delete(reading_state).where(reading_state.c.paper_id == "B"))
    with pytest.raises(IntegrityError, match="paper_relation_library_required"):
        store.relations.add_batch(batch())
    assert store.relations.batches_for_paper("A") == ()


def test_concurrent_batch_and_admission_retries_preserve_first_complete_records(store):
    barrier = Barrier(2)

    def add(index):
        other = SqlStore(str(store._db._engine.url))
        try:
            barrier.wait(timeout=5)
            admission = other.admissions.add("A", {"worker": index}, "t")
            saved = other.relations.add_batch(batch(f"worker-{index}", input_hash="same"))
            return admission, saved
        finally:
            other.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(add, range(2)))
    assert results[0] == results[1]
    assert len(store.relations.for_paper("A")) == 1


def test_stale_snapshot_includes_all_batches_and_status_history_outside_transaction(
    store, monkeypatch
):
    store.relations.add_batch(batch("old"))
    store.relations.add_batch(batch("new", created_at="t2"))
    convert = stale_repo._snapshot

    def outside(rows):
        assert store._db._active is None
        return convert(rows)

    monkeypatch.setattr(stale_repo, "_snapshot", outside)
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(store._db._engine, "after_cursor_execute", capture)
    try:
        snapshot = store.stale_snapshot()
    finally:
        event.remove(store._db._engine, "after_cursor_execute", capture)
    assert len([sql for sql in statements if sql.startswith("SELECT")]) == 18
    assert snapshot.relations == tuple(reversed(store.relations.for_paper("A")))
    assert snapshot.relate_batches == tuple(reversed(store.relations.batches_for_paper("A")))
    assert snapshot.library_paper_ids == frozenset({"A", "B", "C"})
    assert snapshot.embeddings == () and snapshot.embedding_library_ids == frozenset()
    assert snapshot.user_decided_pairs == frozenset()


def test_stale_snapshot_tracks_confirmed_model_pairs_without_embeddings_or_extra_selects(store):
    store.relations.add_batch(batch())
    relation = store.relations.for_paper("A")[0]
    decided = decide(store, relation, "confirm")
    assert decided.source == "model"
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(store._db._engine, "after_cursor_execute", capture)
    try:
        snapshot = store.stale_snapshot()
    finally:
        event.remove(store._db._engine, "after_cursor_execute", capture)
    assert len([sql for sql in statements if sql.startswith("SELECT")]) == 18
    assert snapshot.user_decided_pairs == frozenset({("A", "B")})
    assert snapshot.user_decided_relation_ids == frozenset({relation.id})
    assert snapshot.library_paper_ids == frozenset({"A", "B", "C"})
    assert snapshot.embedding_library_ids == frozenset()


def test_old_feedback_replay_cannot_restore_prior_action_even_with_same_timestamp(store):
    store.relations.add_batch(batch())
    relation = store.relations.for_paper("A")[0]
    decide(store, relation, "confirm", feedback_id="z-first")
    rejected = decide(store, relation, "reject", feedback_id="a-later")
    with pytest.raises(ValueError, match="relation_feedback_not_latest"):
        store.relations.apply_feedback(relation.id, "confirm", "z-first", None, "t2")
    assert store.relations.get(relation.id) == rejected


def test_stale_decisions_match_exact_nonsuperseded_record_and_preserve_history(store):
    store.relations.add_batch(batch("old"))
    old = store.relations.for_paper("A")[0]
    store.relations.add_batch(batch("current", created_at="t1"))
    current = store.relations.for_paper("A")[0]
    decide(store, current, "confirm")
    snapshot = store.stale_snapshot()
    assert snapshot.user_decided_pairs == frozenset({("A", "B")})
    assert snapshot.user_decided_relation_ids == frozenset({current.id})
    assert old.id not in snapshot.user_decided_relation_ids
    decide(store, current, "change", "same_problem", feedback_id="change")
    assert store.stale_snapshot().user_decided_relation_ids == frozenset()
