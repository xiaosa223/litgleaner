"""阅读记忆一次读取事实、相关判断和用户正文，固定十一组短事务查询。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Event

import pytest
from sqlalchemy import delete, event
from tests.integration.test_export_repo import paper
from tests.integration.test_relation_repo import batch, proposal

from litgleaner.domain.identity import Alias
from litgleaner.domain.reading import NewAnnotation
from litgleaner.infra.db import reading_memory_repo
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import reading_state
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.feedback import FeedbackRecord
from litgleaner.ports.memory import MemorySnapshot
from litgleaner.ports.reading_memory import ReadingMemorySnapshot
from litgleaner.ports.store import FullTextState


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'reading-memory.db'}"
    upgrade_database(url)
    result = SqlStore(url)
    yield result
    result.dispose()


def _papers(store, *ids):
    for id_ in ids:
        store.papers.insert(paper(id_))
        store.reader.set_state(id_, "read", None, "t")


def _relation(store, a="A", b="B", id_="batch", *, extra=(), kind="extends"):
    ids = (a, b, *extra)
    saved = store.relations.add_batch(
        batch(
            id_,
            paper_id=a,
            relations=(proposal(a, b, kind),),
            source_hashes={id_: f"source-{id_}" for id_ in ids},
            considered_paper_ids=ids,
            input_paper_ids=ids,
        )
    )
    return next(row for row in store.relations.for_paper(a) if row.batch_id == saved.id)


def _note(store, id_="B", body="用户原话", *, quote=None, kind="note", page=None):
    return store.reader.add_annotation(NewAnnotation(id_, kind, page, None, quote, body), "t")


def decide(store, relation, action, kind=None, *, feedback_id="feedback"):
    record = FeedbackRecord(
        feedback_id,
        relation.paper_a,
        "reader",
        action,
        external_id=feedback_id,
        relation_id=relation.id,
        payload={"kind": kind},
    )
    with store.atomic():
        store.feedback.add(record, "t2")
        return store.relations.apply_feedback(relation.id, action, feedback_id, kind, "t2")


def test_empty_snapshot_is_frozen_and_uses_exact_contract(store):
    loaded = store.reading_memory_snapshot("missing")
    assert loaded == ReadingMemorySnapshot(
        MemorySnapshot((), {}, (), {}, (), (), (), {}), (), (), (), frozenset()
    )
    with pytest.raises(FrozenInstanceError):
        loaded.relations = ()


def test_fact_scope_is_reused_with_global_aliases_and_complete_fulltext_pointers(store):
    _papers(store, "A", "B", "C")
    _relation(store, extra=("C",))
    for id_ in ("A", "B", "C"):
        store.papers.save_fulltext(
            FullTextState(id_, "parsed", "sha", f"doc-{id_}", "provider", "license", "good"),
            "t",
        )
    store.papers.add_aliases("C", (Alias("doi", "10.1000/conflict"),))
    store._db.write(delete(reading_state).where(reading_state.c.paper_id == "C"))
    original = store.memory_snapshot()
    loaded = store.reading_memory_snapshot("A")
    assert {row.id for row in loaded.facts.papers} == {"A", "B"}
    assert loaded.facts.aliases["C"] == (Alias("doi", "10.1000/conflict"),)
    assert set(original.fulltexts) == {"A", "B"}
    assert set(loaded.facts.fulltexts) == {"A", "B", "C"}
    assert loaded.facts == replace(
        original, fulltexts={id_: store.papers.get_fulltext(id_) for id_ in ("A", "B", "C")}
    )
    assert loaded.batches[0].input_paper_ids == ("A", "B", "C")
    assert loaded.facts.fulltexts["C"].doc_key == "doc-C"


def test_only_valid_main_relations_and_their_original_batches_are_loaded(store):
    _papers(store, "A", "B", "C", "D")
    old = _relation(store, id_="old")
    current = _relation(store, id_="current", kind="compares_with")
    reverse = _relation(store, "C", "A", "reverse")
    _relation(store, "C", "D", "unrelated")
    rejected = _relation(store, "A", "D", "rejected")
    decide(store, rejected, "reject")
    store.relations.add_batch(batch("empty", relations=()))
    loaded = store.reading_memory_snapshot("A")
    assert {row.id: row for row in loaded.relations} == {current.id: current, reverse.id: reverse}
    assert {row.id for row in loaded.batches} == {"current", "reverse"}
    assert all(row.status == "valid" for row in loaded.relations)
    assert old.id not in {row.id for row in loaded.relations}
    assert reverse.paper_a == "C" and reverse.paper_b == "A"


def test_more_than_fifty_relations_and_batches_are_not_pretruncated(store):
    _papers(store, "A", *(f"P{index:03}" for index in range(70)))
    with store.atomic():
        expected = {
            _relation(store, "A", f"P{index:03}", f"batch-{index:03}").id for index in range(70)
        }
    loaded = store.reading_memory_snapshot("A")
    assert {row.id for row in loaded.relations} == expected
    assert len(loaded.batches) == 70
    assert tuple(row.id for row in loaded.batches) == tuple(
        f"batch-{index:03}" for index in range(70)
    )


def test_user_decisions_apply_to_exact_current_record_and_changed_user_judgment(store):
    _papers(store, "A", "B", "C", "D")
    old = _relation(store, id_="old")
    current = _relation(store, id_="current")
    other = _relation(store, "C", "D", "other")
    decide(store, other, "confirm", feedback_id="other-confirm")
    assert store.reading_memory_snapshot("A").user_decided_relation_ids == frozenset()
    decide(store, current, "confirm", feedback_id="current-confirm")
    loaded = store.reading_memory_snapshot("A")
    assert loaded.user_decided_relation_ids == frozenset({current.id})
    assert old.id not in loaded.user_decided_relation_ids
    changed = decide(store, current, "change", "same_problem", feedback_id="change")
    loaded = store.reading_memory_snapshot("A")
    assert loaded.relations == (changed,)
    assert loaded.user_decided_relation_ids == frozenset({changed.id})
    decide(store, changed, "reject", feedback_id="reject")
    assert store.reading_memory_snapshot("A").relations == ()
    assert store.reading_memory_snapshot("A").user_decided_relation_ids == frozenset()


def test_annotations_only_keep_current_nonempty_user_body_and_preserve_metadata(store):
    _papers(store, "A", "B")
    note = _note(store, body=" 首行原话\n第二行 ")
    highlight = _note(store, body="高亮旁的用户正文", quote="作者原话", kind="highlight", page=4)
    for body in (None, "", " \t\r\n", "\u3000"):
        _note(store, body=body, quote="没有用户正文的作者高亮", kind="highlight")
    deleted = _note(store, body="deleted")
    store.reader.delete_annotation(deleted.id, "t2")
    loaded = store.reading_memory_snapshot("A")
    assert loaded.annotations == tuple(sorted((note, highlight), key=lambda row: row.id))
    assert loaded.annotations[0].body in {note.body, highlight.body}
    store.reader.update_annotation(note.id, "更新的用户原话", "t3")
    updated = store.reading_memory_snapshot("A")
    assert next(row for row in updated.annotations if row.id == note.id).body == "更新的用户原话"
    assert next(row for row in updated.annotations if row.id == highlight.id).page == 4
    store.reader.delete_annotation(note.id, "t4")
    assert store.reading_memory_snapshot("A").annotations == (highlight,)


@pytest.mark.parametrize("count", [0, 75, 1100])
def test_exact_eleven_selects_share_one_transaction_and_convert_after_commit(
    store, count, monkeypatch
):
    with store.atomic():
        _papers(store, *(f"P{index:04}" for index in range(count)))
    with store._db._engine.connect() as connection:
        connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    statements, connections = [], set()
    convert = reading_memory_repo._snapshot

    def capture(conn, _cursor, sql, params, _context, _many):
        statements.append((sql, params))
        connections.add(id(conn))
        if sql.startswith("SELECT"):
            assert store._db._active is conn

    def outside(rows):
        assert store._db._active is None
        return convert(rows)

    def forbidden(_statement):
        pytest.fail("reading memory snapshot must not write")

    monkeypatch.setattr(store._db, "write", forbidden)
    monkeypatch.setattr(reading_memory_repo, "_snapshot", outside)
    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        loaded = store.reading_memory_snapshot("P0000")
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    selects = [(sql, params) for sql, params in statements if sql.startswith("SELECT")]
    assert len(selects) == 11 and len(connections) == 1
    assert all(len(params) <= 5 and "LIMIT" not in sql for sql, params in selects)
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql, _ in statements)
    assert len(loaded.facts.papers) == count and len(loaded.facts.reading) == count


def test_concurrent_feedback_note_and_reading_changes_do_not_mix_snapshot_versions(store):
    _papers(store, "A", "B")
    relation = _relation(store)
    note = _note(store, body="before")
    writer = SqlStore(str(store._db._engine.url))
    started, futures = Event(), []

    def change():
        started.set()
        with writer.atomic():
            writer.reader.update_annotation(note.id, "after", "t2")
            writer.reader.set_state("B", "abandoned", None, "t2")
            decide(writer, relation, "reject")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:

            def capture(_conn, _cursor, sql, _params, _context, _many):
                if sql.startswith("SELECT") and not futures:
                    futures.append(executor.submit(change))
                    assert started.wait(5)

            event.listen(store._db._engine, "after_cursor_execute", capture)
            try:
                before = store.reading_memory_snapshot("A")
            finally:
                event.remove(store._db._engine, "after_cursor_execute", capture)
            assert before.relations == (relation,) and before.annotations == (note,)
            assert before.facts.reading["B"].status == "read"
            futures[0].result(timeout=5)
        after = store.reading_memory_snapshot("A")
        assert after.relations == () and after.batches == ()
        assert after.annotations[0].body == "after"
        assert after.facts.reading["B"].status == "abandoned"
    finally:
        writer.dispose()


@pytest.mark.parametrize("stage", [4, 9, 10, 11])
def test_failed_read_releases_transaction_and_next_snapshot_recovers(store, monkeypatch, stage):
    _papers(store, "A", "B")
    relation = _relation(store)
    fetch, count = store._db.fetch, 0

    def fail(statement):
        nonlocal count
        count += 1
        if count == stage:
            raise RuntimeError("query failed")
        return fetch(statement)

    with monkeypatch.context() as patch:
        patch.setattr(store._db, "fetch", fail)
        with pytest.raises(RuntimeError, match="query failed"):
            store.reading_memory_snapshot("A")
    assert store._db._active is None
    assert store.reading_memory_snapshot("A").relations == (relation,)
