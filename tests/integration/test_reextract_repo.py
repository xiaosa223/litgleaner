"""历史重提取窄快照：完整身份、稳定范围、锁外转换与并发一致。"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Event

import pytest
from sqlalchemy import delete, event, insert, update

from litgleaner.domain.paper import Paper
from litgleaner.infra.db import reextract_repo
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import extraction, fulltext, job, paper
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.reextract import ReextractSnapshot, metadata_revision
from litgleaner.ports.store import FullTextState, JobSpec


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'reextract.db'}"
    upgrade_database(url)
    result = SqlStore(url)
    yield result
    result.dispose()


def _paper(store, id_, *, history=True):
    result = Paper(id_, f"Study {id_}", id_, "Original abstract", (), None, None, None, "t")
    store.papers.insert(result)
    store.papers.save_fulltext(
        FullTextState(id_, "parsed", "sha", f"doc-{id_}", "source", "license", "ok"), "t"
    )
    if history:
        store.papers.insert_extraction(id_, "input", "producer", {"summary": "old data"}, "t")
    return result


def _raw_job(store, key, payload, *, stage="reextract", status="pending"):
    store._db.write(
        insert(job).values(
            key=key,
            stage=stage,
            payload=payload,
            status=status,
            attempts=2,
            next_run_at="t",
            last_error="historical error",
            first_deferred_at="deferred",
            created_at="t",
            updated_at="t",
        )
    )


def test_empty_snapshot_is_frozen_and_revision_matches_standalone_check(store):
    result = store.reextract_snapshot()
    assert result == ReextractSnapshot((), {}, (), (), metadata_revision((), {}, ()))
    assert store.reextract_revision() == result.revision
    with pytest.raises(FrozenInstanceError):
        result.revision = "changed"


def test_default_includes_all_history_without_requiring_library_membership(store):
    expected = _paper(store, "A")
    _paper(store, "B", history=False)
    _paper(store, "C")
    for index in range(75):
        store.papers.insert_extraction("A", f"input-{index}", "producer", {}, "t")
    loaded = store.reextract_snapshot()
    assert loaded.papers[0] == expected
    assert tuple(p.id for p in loaded.papers) == ("A", "C")
    assert set(loaded.fulltexts) == {"A", "C"}
    assert loaded.fulltexts["A"] == store.papers.get_fulltext("A")
    assert len(loaded.extractions) == 77
    assert [row.id for row in loaded.extractions] == sorted(row.id for row in loaded.extractions)
    assert all(not hasattr(row, "data") for row in loaded.extractions)


@pytest.mark.parametrize(
    "ids,expected",
    [((), ()), (("missing",), ()), (("B",), ("B",)), (("B", "A", "A", "missing"), ("A", "B"))],
)
def test_explicit_scope_preserves_existing_without_extraction_and_omits_unknown(
    store, ids, expected
):
    _paper(store, "A")
    _paper(store, "B", history=False)
    result = store.reextract_snapshot(ids)
    assert tuple(p.id for p in result.papers) == expected
    assert set(result.fulltexts) == set(expected)
    assert {row.paper_id for row in result.extractions} == ({"A"} & set(expected))
    assert result.revision == store.reextract_revision(ids)


def test_jobs_preserve_all_states_and_skip_wrong_stage_bad_json_and_invalid_scope(store):
    _paper(store, "A")
    _paper(store, "B", history=False)
    for state in ("pending", "done", "dead"):
        _raw_job(store, state, '{"paper_id":"A","manifest":{"x":1}}', status=state)
    invalid = [
        "{broken",
        "null",
        "[]",
        '"text"',
        "42",
        "{}",
        '{"paper_id":true}',
        '{"paper_id":[]}',
        '{"paper_id":" "}',
        '{"paper_id":"missing"}',
        '{"paper_id":"A","large":' + "9" * 5000 + "}",
        '{"paper_id":"A","deep":' + "[" * 2000 + "0" + "]" * 2000 + "}",
    ]
    for index, payload in enumerate(invalid):
        _raw_job(store, f"bad-{index}", payload)
    _raw_job(store, "wrong-stage", '{"paper_id":"A"}', stage="extract")
    _raw_job(store, "no-history", '{"paper_id":"B"}')
    result = store.reextract_snapshot()
    assert tuple(item.key for item in result.jobs) == ("dead", "done", "pending")
    assert {item.status for item in result.jobs} == {"pending", "done", "dead"}
    assert all(item.attempts == 2 and item.first_deferred_at == "deferred" for item in result.jobs)
    assert result.jobs[0].payload["manifest"] == {"x": 1}
    assert result.jobs[0].last_error == "historical error"
    assert tuple(item.key for item in store.reextract_snapshot(("B",)).jobs) == ("no-history",)


@pytest.mark.parametrize("ids", [None, ("A",)])
def test_sql_filters_unrelated_large_and_bad_payloads_before_conversion(store, monkeypatch, ids):
    _paper(store, "A")
    _paper(store, "B", history=False)
    _raw_job(store, "accepted", '{"paper_id":"A"}')
    _raw_job(store, "large-other", json.dumps({"paper_id": "B", "inputs": "x" * 100_000}))
    _raw_job(store, "malformed", '{"paper_id":"A",broken')
    _raw_job(store, "not-text", '{"paper_id":true}')
    convert = reextract_repo._jobs

    def observed(rows, scope):
        assert tuple(row.key for row in rows) == ("accepted",)
        return convert(rows, scope)

    monkeypatch.setattr(reextract_repo, "_jobs", observed)
    assert store.reextract_snapshot(ids).jobs[0].key == "accepted"


@pytest.mark.parametrize("count", [0, 75, 1100])
@pytest.mark.parametrize("method,expected", [("reextract_snapshot", 4), ("reextract_revision", 3)])
def test_fixed_queries_share_transaction_and_convert_outside_it(
    store, monkeypatch, count, method, expected
):
    with store.atomic():
        for index in range(count):
            _paper(store, f"P{index:04}")
    with store._db._engine.connect() as connection:
        connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    statements, connections = [], set()
    convert = reextract_repo._snapshot

    def capture(conn, _cursor, sql, params, _context, _many):
        statements.append((sql, params))
        connections.add(id(conn))
        assert store._db._active is conn if sql.startswith("SELECT") else True

    def outside(rows, jobs=()):
        assert store._db._active is None
        assert len(rows.papers) == count
        return convert(rows, jobs)

    def forbidden(_statement):
        pytest.fail("snapshot may not write")

    monkeypatch.setattr(store._db, "write", forbidden)
    monkeypatch.setattr(reextract_repo, "_snapshot", outside)
    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        getattr(store, method)(tuple(f"P{index:04}" for index in range(count)))
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    selects = [(sql, params) for sql, params in statements if sql.startswith("SELECT")]
    assert len(selects) == expected and len(connections) == 1
    assert all(len(params) <= 5 and "LIMIT" not in sql for sql, params in selects)
    assert "extraction.data" not in selects[2][0]
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql, _ in statements)


def test_old_extraction_data_is_not_decoded_and_helper_can_reproduce_single_paper(
    store, monkeypatch
):
    _paper(store, "A")
    _paper(store, "B")
    decode = json.loads

    def guarded(value, *args, **kwargs):
        assert "old data" not in value
        return decode(value, *args, **kwargs)

    monkeypatch.setattr(reextract_repo.json, "loads", guarded)
    all_ = store.reextract_snapshot()
    revision = metadata_revision(
        tuple(p for p in all_.papers if p.id == "A"),
        {"A": all_.fulltexts["A"]},
        tuple(row for row in all_.extractions if row.paper_id == "A"),
    )
    assert revision == store.reextract_revision(("A",))
    assert (
        metadata_revision(all_.papers[::-1], all_.fulltexts, all_.extractions[::-1])
        == all_.revision
    )


def test_job_state_does_not_change_revision_and_unrelated_sources_do_not_change_single_scope(store):
    _paper(store, "A")
    before = store.reextract_snapshot().revision
    store.jobs.enqueue(JobSpec("reextract:A", "reextract", {"paper_id": "A"}), "t")
    store.jobs.mark_dead("reextract:A", "failed", "t2")
    assert store.reextract_snapshot().revision == before
    store.jobs.reopen("reextract:A", "t3")
    store.jobs.mark_done("reextract:A", "t4")
    assert store.reextract_revision() == before
    _paper(store, "B")
    assert store.reextract_revision(("A",)) == before
    assert store.reextract_revision() != before


@pytest.mark.parametrize("kind", ["paper", "fulltext", "new_extraction", "delete_extraction"])
def test_canonical_source_changes_modify_revision(store, kind):
    _paper(store, "A")
    before = store.reextract_revision()
    if kind == "paper":
        store._db.write(update(paper).where(paper.c.id == "A").values(abstract="Changed"))
    elif kind == "fulltext":
        state = store.papers.get_fulltext("A")
        store.papers.save_fulltext(replace(state, doc_key="new-doc"), "t2")
    elif kind == "new_extraction":
        store.papers.insert_extraction("A", "new-input", "producer", {}, "t2")
    else:
        store._db.write(delete(extraction).where(extraction.c.paper_id == "A"))
    assert store.reextract_revision() != before


def test_revision_inside_existing_write_transaction_uses_uncommitted_canonical_rows(store):
    _paper(store, "A")
    original = store.reextract_revision(("A",))
    with store.atomic():
        store._db.write(update(fulltext).where(fulltext.c.paper_id == "A").values(quality="new"))
        assert store.reextract_revision(("A",)) != original
        assert store.reextract_revision(("A",)) == store.reextract_snapshot(("A",)).revision
    assert store.reextract_revision(("A",)) != original


@pytest.mark.parametrize("stage", [1, 2, 3, 4])
def test_read_failure_releases_transaction_and_later_snapshot_recovers(store, monkeypatch, stage):
    _paper(store, "A")
    fetch, count = store._db.fetch, 0

    def fail(statement):
        nonlocal count
        count += 1
        if count == stage:
            raise RuntimeError("read failure")
        return fetch(statement)

    monkeypatch.setattr(store._db, "fetch", fail)
    with pytest.raises(RuntimeError, match="read failure"):
        store.reextract_snapshot()
    assert store._db._active is None
    monkeypatch.setattr(store._db, "fetch", fetch)
    assert store.reextract_snapshot().papers[0].id == "A"


def test_conversion_failure_occurs_after_transaction_release(store, monkeypatch):
    _paper(store, "A")
    convert = reextract_repo._snapshot

    def failed(_rows, _jobs=()):
        assert store._db._active is None
        raise ValueError("convert failure")

    monkeypatch.setattr(reextract_repo, "_snapshot", failed)
    with pytest.raises(ValueError, match="convert failure"):
        store.reextract_snapshot()
    monkeypatch.setattr(reextract_repo, "_snapshot", convert)
    assert store.reextract_revision() == store.reextract_snapshot().revision


def test_concurrent_writer_cannot_mix_paper_pointer_history_and_job_versions(store):
    _paper(store, "A")
    before = store.reextract_snapshot()
    writer = SqlStore(str(store._db._engine.url))
    started, futures = Event(), []

    def change():
        started.set()
        with writer.atomic():
            writer._db.write(update(paper).where(paper.c.id == "A").values(abstract="Changed"))
            writer._db.write(
                update(fulltext).where(fulltext.c.paper_id == "A").values(doc_key="new")
            )
            writer.papers.insert_extraction("A", "new", "producer", {}, "t2")
            writer.jobs.enqueue(JobSpec("new", "reextract", {"paper_id": "A"}), "t2")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:

            def capture(_conn, _cursor, sql, _params, _context, _many):
                if sql.startswith("SELECT") and not futures:
                    futures.append(executor.submit(change))
                    assert started.wait(5)

            event.listen(store._db._engine, "after_cursor_execute", capture)
            try:
                during = store.reextract_snapshot()
            finally:
                event.remove(store._db._engine, "after_cursor_execute", capture)
            assert during == before
            futures[0].result(timeout=5)
        after = store.reextract_snapshot()
        assert after.papers[0].abstract == "Changed" and after.fulltexts["A"].doc_key == "new"
        assert len(after.extractions) == 2 and after.jobs[0].key == "new"
        assert after.revision != before.revision
    finally:
        writer.dispose()
