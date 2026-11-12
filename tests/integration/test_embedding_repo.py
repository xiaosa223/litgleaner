"""本地向量的 SQL 边界、只追加幂等、迁移与一致批量快照。"""

import sqlite3
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from threading import Barrier, Event

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from tests.integration import relation_migration_helpers as migrations
from tests.integration.test_export_repo import paper
from tests.integration.test_memory_repo import add_inbox, add_note, add_paper
from tests.integration.test_outline_migration import _legacy_map
from tests.integration.test_outline_repo import result as outline_result
from tests.integration.test_topic_map_migration import _snapshot as db_snapshot

from litgleaner.domain.embedding import PaperEmbedding, pack_vector
from litgleaner.infra.db import embedding_repo
from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.infra.db.schema import metadata
from litgleaner.infra.db.schema_rules import include_schema_object
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.embedding import EmbeddingSnapshot
from litgleaner.ports.store import FullTextState


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'embedding.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    yield store
    store.dispose()


def record(paper_id="P", **changes):
    return replace(
        PaperEmbedding(
            paper_id,
            "input",
            "producer",
            "configuration",
            "local-model",
            2,
            "Title: 原始标题\nAbstract: original",
            pack_vector((0.5, -1.0), 2),
            None,
            "t",
        ),
        **changes,
    )


def raw_insert(store, item, prefix="INSERT", **changes):
    fields = {**asdict(item), **changes}
    statement = text(
        f"{prefix} INTO paper_embedding ({','.join(fields)}) "
        f"VALUES ({','.join(':' + name for name in fields)})"
    ).bindparams(**fields)
    return store._db.write(statement)


def test_roundtrip_duplicate_and_conflict_never_overwrite_original(store):
    add_paper(store, "P", "queued")
    store.papers.insert_extraction("P", "source", "extractor", {"problem": "原文"}, "t")
    original = record(source_extraction_id=store.papers.get_extraction("P").id)
    assert store.embeddings.add(original) is True
    assert store.embeddings.add(original) is False
    assert store.embeddings.add(replace(original, input_text="changed", vector=b"broken")) is False
    assert store.embeddings.for_paper("P") == (original,)
    assert store.embeddings.for_paper("missing") == ()
    assert store.embeddings.snapshot().embeddings == (original,)


def test_sql_source_extraction_must_belong_to_the_same_paper(store):
    add_paper(store, "P")
    add_paper(store, "other")
    store.papers.insert_extraction("other", "source", "extractor", {}, "t")
    source_id = store.papers.get_extraction("other").id
    with pytest.raises(IntegrityError, match="paper_embedding_source_invalid"):
        raw_insert(store, record(source_extraction_id=source_id))
    assert store.embeddings.for_paper("P") == ()
    assert store.embeddings.add(record("other", source_extraction_id=source_id))
    plan = store._db.fetch(
        text(
            "EXPLAIN QUERY PLAN SELECT 1 FROM paper_embedding WHERE source_extraction_id=:id"
        ).bindparams(id=source_id)
    )
    assert any("ix_paper_embedding_source_extraction_id" in row[3] for row in plan)


def test_history_is_complete_and_stably_sorted_without_default_fifty_window(store):
    add_paper(store, "P", "read")
    items = [record(input_hash=f"input-{n:03}") for n in reversed(range(55))]
    newest = record(input_hash="new", created_at="t2")
    alternate = record(input_hash="input-001", embedder_version="alternate")
    for item in [*items, newest, alternate]:
        assert store.embeddings.add(item)
    expected = (newest, items[-1], alternate, *list(reversed(items))[1:])
    assert store.embeddings.for_paper("P") == expected
    assert store.embeddings.snapshot().embeddings == expected


@pytest.mark.parametrize(
    "field",
    [
        "paper_id",
        "input_hash",
        "embedder_version",
        "configuration_version",
        "model",
        "input_text",
        "created_at",
    ],
)
@pytest.mark.parametrize("value", ["", "   ", None])
def test_sql_requires_nonempty_identity_and_actual_input(store, field, value):
    add_paper(store, "P")
    with pytest.raises(IntegrityError):
        raw_insert(store, record(), **{field: value})


@pytest.mark.parametrize(
    "changes",
    [
        {"dimensions": 0},
        {"dimensions": -1},
        {"dimensions": 65537},
        {"dimensions": 2.5},
        {"dimensions": None},
        {"vector": b"short"},
        {"vector": b""},
        {"vector": "12345678"},
        {"vector": None},
        {"paper_id": "missing"},
        {"source_extraction_id": 999},
    ],
)
def test_sql_enforces_dimension_blob_length_type_and_foreign_keys(store, changes):
    add_paper(store, "P")
    with pytest.raises(IntegrityError):
        raw_insert(store, record(), **changes)


@pytest.mark.parametrize("dimensions", [1, 65536])
def test_boundary_dimensions_roundtrip_as_little_endian_float32(store, dimensions):
    add_paper(store, "P")
    item = record(dimensions=dimensions, vector=pack_vector((1.0,) * dimensions, dimensions))
    assert store.embeddings.add(item)
    assert store.embeddings.for_paper("P") == (item,)


@pytest.mark.parametrize("values", [(0.0, 0.0), (float("inf"), 1.0), (float("nan"), 1.0)])
def test_repository_rejects_nonfinite_or_zero_vectors_before_any_insert(store, values):
    add_paper(store, "P")
    with pytest.raises(ValueError):
        store.embeddings.add(record(vector=struct.pack("<2f", *values)))
    assert store.embeddings.for_paper("P") == ()


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE paper_embedding SET input_text='changed'",
        "UPDATE OR IGNORE paper_embedding SET input_text='changed'",
        "UPDATE OR REPLACE paper_embedding SET input_hash='changed'",
        "DELETE FROM paper_embedding",
    ],
)
def test_sql_updates_and_deletes_cannot_bypass_immutability(store, sql):
    add_paper(store, "P")
    original = record()
    store.embeddings.add(original)
    with pytest.raises(IntegrityError, match="paper_embedding_immutable"):
        store._db.write(text(sql))
    assert store.embeddings.for_paper("P") == (original,)


@pytest.mark.parametrize("prefix", ["INSERT", "INSERT OR IGNORE", "INSERT OR REPLACE", "REPLACE"])
def test_duplicate_inserts_cannot_bypass_immutability_with_conflict_policy(store, prefix):
    add_paper(store, "P")
    original = record()
    store.embeddings.add(original)
    with pytest.raises(IntegrityError, match="paper_embedding_immutable"):
        raw_insert(store, replace(original, input_text="changed"), prefix)
    assert store.embeddings.for_paper("P") == (original,)


def test_replace_cannot_delete_existing_record_through_hidden_rowid_conflict(store):
    add_paper(store, "P")
    original = record()
    store.embeddings.add(original)
    with pytest.raises(IntegrityError, match="paper_embedding_immutable"):
        raw_insert(store, record(input_hash="different"), "INSERT OR REPLACE", rowid=1)
    assert store.embeddings.for_paper("P") == (original,)


@pytest.mark.parametrize("rowid", [-99, -1, 0])
def test_nonpositive_rowid_is_atomically_rejected_and_normal_append_still_succeeds(store, rowid):
    add_paper(store, "P")
    with pytest.raises(IntegrityError, match="paper_embedding_rowid_invalid"):
        raw_insert(store, record(), rowid=rowid)
    assert store.embeddings.for_paper("P") == ()
    assert store.embeddings.add(record())
    assert store.embeddings.for_paper("P") == (record(),)


def test_concurrent_retry_appends_once_and_outer_failure_rolls_back(store):
    add_paper(store, "P", "queued")
    barrier = Barrier(2)

    def add():
        other = SqlStore(str(store._db._engine.url))
        try:
            barrier.wait(timeout=5)
            return other.embeddings.add(record())
        finally:
            other.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: add(), range(2))) == [False, True]
    with pytest.raises(RuntimeError), store.atomic():
        store.embeddings.add(record(input_hash="rolled-back"))
        raise RuntimeError("rollback batch")
    assert store.embeddings.for_paper("P") == (record(),)


def test_narrow_snapshot_scope_latest_extraction_and_fulltext_preserve_all_history(store):
    for paper_id in ("A", "B", "C", "external", "deleted", "pending"):
        add_paper(store, paper_id, "abandoned" if paper_id == "A" else None)
        store.embeddings.add(record(paper_id))
        store.embeddings.add(record(paper_id, input_hash="older", created_at="a"))
        store.papers.insert_extraction(paper_id, "current", "v", {"valid": True}, "t9")
        store.papers.insert_extraction(paper_id, "old", "old", {}, "a")
        store.papers.save_fulltext(FullTextState(paper_id, "parsed", "sha", "doc"), "t")
    add_note(store, "B")
    add_inbox(store, "C", "done")
    add_inbox(store, "pending", "pending")
    deleted = add_note(store, "deleted")
    store.reader.delete_annotation(deleted.id, "t2")
    loaded = store.embeddings.snapshot()
    assert loaded.papers == tuple(paper(id_) for id_ in ("A", "B", "C"))
    assert loaded.fulltexts == {key: store.papers.get_fulltext(key) for key in ("A", "B", "C")}
    assert loaded.extractions == tuple(store.papers.get_extraction(key) for key in ("A", "B", "C"))
    assert all(item.data == {} for item in loaded.extractions)
    assert loaded.embeddings == tuple(
        item for key in ("A", "B", "C") for item in store.embeddings.for_paper(key)
    )
    assert loaded == store.embeddings.snapshot()


@pytest.mark.parametrize("count", [0, 55, 1100])
def test_snapshot_uses_four_selects_one_short_transaction_no_writes(store, count, monkeypatch):
    with store.atomic():
        for n in reversed(range(count)):
            paper_id = f"P{n:04}"
            add_paper(store, paper_id, "read")
            store.embeddings.add(record(paper_id))
    with store._db._engine.connect() as connection:
        connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    statements, connections = [], set()
    convert = embedding_repo._snapshot

    def capture(conn, _cursor, sql, params, _context, _many):
        statements.append((sql, params))
        connections.add(id(conn))
        if sql.startswith("SELECT"):
            assert store._db._active is conn

    def outside_transaction(rows):
        assert store._db._active is None
        return convert(rows)

    monkeypatch.setattr(embedding_repo, "_snapshot", outside_transaction)
    monkeypatch.setattr(store._db, "write", lambda _: pytest.fail("snapshot must not write"))
    event.listen(store._db._engine, "after_cursor_execute", capture)
    try:
        loaded = store.embeddings.snapshot()
    finally:
        event.remove(store._db._engine, "after_cursor_execute", capture)
    selects = [(sql, params) for sql, params in statements if sql.startswith("SELECT")]
    assert len(selects) == 4 and len(connections) == 1
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql, _ in statements)
    assert all(len(params) <= 1 and "LIMIT" not in sql for sql, params in selects)
    assert tuple(item.id for item in loaded.papers) == tuple(f"P{n:04}" for n in range(count))
    assert len(loaded.embeddings) == count
    if count == 0:
        assert loaded == EmbeddingSnapshot((), {}, (), ())


def test_stale_snapshot_keeps_all_embedding_history_and_projects_current_library_membership(store):
    for paper_id in ("A", "external"):
        add_paper(store, paper_id, "queued" if paper_id == "A" else None)
        store.embeddings.add(record(paper_id))
        store.embeddings.add(record(paper_id, input_hash="older", created_at="a"))
    loaded = store.stale_snapshot()
    assert loaded.embedding_library_ids == frozenset({"A"})
    assert loaded.embeddings == tuple(
        item for key in ("A", "external") for item in reversed(store.embeddings.for_paper(key))
    )
    assert len(loaded.embeddings) == 4


def test_snapshot_scope_and_input_cannot_mix_with_concurrent_changes(store):
    add_paper(store, "P", "queued")
    add_paper(store, "Q")
    writer = SqlStore(str(store._db._engine.url))
    started, futures = Event(), []

    def change():
        started.set()
        with writer.atomic():
            writer.papers.fill_missing("P", {"abstract": "Changed"})
            writer.reader.set_state("Q", "read", None, "t2")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:

            def capture(_conn, _cursor, sql, _params, _context, _many):
                if sql.startswith("SELECT") and not futures:
                    futures.append(executor.submit(change))
                    assert started.wait(5)

            event.listen(store._db._engine, "after_cursor_execute", capture)
            try:
                loaded = store.embeddings.snapshot()
            finally:
                event.remove(store._db._engine, "after_cursor_execute", capture)
            assert loaded.papers == (paper("P"),)
            futures[0].result(timeout=5)
        updated = store.embeddings.snapshot()
        assert tuple(item.id for item in updated.papers) == ("P", "Q")
        assert updated.papers[0].abstract == "Changed"
    finally:
        writer.dispose()


def test_0018_preserves_prior_facts_schema_fts_keys_triggers_and_roundtrips(tmp_path):
    path = tmp_path / "legacy.db"
    url = _legacy_map(path)
    command.upgrade(alembic_config(url), "0017")
    store = SqlStore(url)
    store.topic_outlines.add(outline_result())
    store.dispose()
    before, keys, definitions, triggers = db_snapshot(path)
    upgrade_database(url)
    upgrade_database(url)
    after, current_keys, current_definitions, current_triggers = db_snapshot(path, before)
    migrations.assert_legacy_preserved(
        before,
        after,
        keys,
        current_keys,
        definitions=definitions,
        current_definitions=current_definitions,
    )
    migrations.assert_search_upgrade_triggers(triggers, current_triggers)
    assert (
        set(current_triggers) - set(triggers)
        == {
            "paper_embedding_no_replace",
            "paper_embedding_no_update",
            "paper_embedding_no_delete",
            "paper_embedding_source",
            "paper_embedding_rowid",
        }
        | migrations.RELATION_TRIGGERS
    )
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"include_object": include_schema_object}
            )
            assert compare_metadata(context, metadata) == []
            assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
            connection.exec_driver_sql(
                "INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')"
            )
    finally:
        engine.dispose()
    store = SqlStore(url)
    store.embeddings.add(record())
    store.dispose()
    command.downgrade(alembic_config(url), "0017")
    assert db_snapshot(path) == (before, keys, definitions, triggers)
    upgrade_database(url)
    store = SqlStore(url)
    try:
        assert store.embeddings.for_paper("P") == ()
        assert store.topic_outlines.get("outline") == outline_result()
    finally:
        store.dispose()
