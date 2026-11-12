"""全局词法与文库向量同一五查询快照；原文、排序和失败隔离保持。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Event

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from tests.integration.test_embedding_repo import record
from tests.integration.test_export_repo import paper
from tests.integration.test_memory_repo import add_inbox, add_note

from litgleaner.domain.document import Block
from litgleaner.domain.reading import NewAnnotation
from litgleaner.infra.db import embedding_repo, search_repo
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.search_index import RANK_SQL
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.embedding import EmbeddingSnapshot
from litgleaner.ports.search import SearchSnapshot
from litgleaner.ports.store import FullTextState


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'hybrid-search.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    yield store
    store.dispose()


def _paper(store, id_, title="Astronomy", *, library=True):
    store.papers.insert(replace(paper(id_), title=title))
    if library:
        store.reader.set_state(id_, "queued", None, "t")


def test_empty_snapshot_and_candidates_have_frozen_contract(store):
    empty = store.search.snapshot("  !!!  ")
    assert empty == SearchSnapshot((), (), EmbeddingSnapshot((), {}, (), ()))
    with pytest.raises(FrozenInstanceError):
        empty.embedding_problem = True
    _paper(store, "P")
    candidate = store.search.snapshot("astronomy").candidates[0]
    with pytest.raises(FrozenInstanceError):
        candidate.bm25 = 1.0


@pytest.mark.parametrize("count", [0, 3, 550])
def test_five_selects_one_connection_no_writes_and_all_conversion_outside(
    store, monkeypatch, count
):
    with store.atomic():
        for n in range(count):
            _paper(store, f"P{n:04}")
    statements, connections, normalized = [], set(), []
    convert, candidate = embedding_repo._snapshot, search_repo._candidate
    normalize = search_repo.normalize_search_text

    def capture(conn, _cursor, sql, params, _context, _many):
        statements.append((sql, params))
        connections.add(id(conn))
        if sql.startswith("SELECT"):
            assert store._db._active is conn

    def outside(rows):
        assert store._db._active is None
        return convert(rows)

    def outside_candidate(row):
        assert store._db._active is None
        return candidate(row)

    def once(value):
        normalized.append(value)
        return normalize(value)

    monkeypatch.setattr(embedding_repo, "_snapshot", outside)
    monkeypatch.setattr(search_repo, "_candidate", outside_candidate)
    monkeypatch.setattr(search_repo, "normalize_search_text", once)
    monkeypatch.setattr(store._db, "write", lambda _: pytest.fail("snapshot must not write"))
    event.listen(store._db._engine, "after_cursor_execute", capture)
    try:
        loaded = store.search.snapshot("  ＡＳＴＲＯＮＯＭＹ  ", limit=999)
    finally:
        event.remove(store._db._engine, "after_cursor_execute", capture)
    assert len([sql for sql, _ in statements if sql.startswith("SELECT")]) == 5
    assert len(connections) == 1 and all(
        sql.startswith(("SELECT", "BEGIN")) for sql, _ in statements
    )
    assert normalized == ["  ＡＳＴＲＯＮＯＭＹ  "] and loaded.terms == ("astronomy",)
    assert len(loaded.candidates) == min(count, 512)
    assert len(loaded.embeddings.papers) == count


def test_raw_full_source_original_bm25_and_old_find_excerpt_remain_accurate(store):
    _paper(store, "P", "Ｓｔｒａße")
    content = "前缀 " * 200 + "Straße 作者原话 é"
    note = store.reader.add_annotation(NewAnnotation("P", "note", 7, None, None, content), "t")
    loaded = store.search.snapshot("ＳＴＲＡＳＳＥ")
    found = next(item for item in loaded.candidates if item.id == note.id + ":body")
    assert found.text == content and (found.page, found.authorship) == (7, "user")
    expected = store._db.fetch(
        text(
            f"SELECT kind,id,{RANK_SQL} AS rank FROM memory_fts "
            "WHERE memory_fts MATCH '{search_title search_text} : (\"strasse\")' "
            "ORDER BY rank,kind,paper_id,id"
        )
    )
    assert [(item.kind, item.id, item.bm25) for item in loaded.candidates] == list(expected)
    assert all(item.bm25 < 0 for item in loaded.candidates)
    old = store.search.find("strasse", 20)
    assert [(item.kind, item.id) for item in old] == [
        (item.kind, item.id) for item in loaded.candidates
    ]
    assert next(item.text for item in old if item.kind == "note") != content


def test_lexical_scope_is_global_but_vectors_keep_library_latest_inputs_and_all_history(store):
    ids = ("read", "note", "inbox", "external", "deleted", "pending")
    for id_ in ids:
        _paper(store, id_, library=id_ == "read")
        store.embeddings.add(record(id_))
        store.embeddings.add(record(id_, input_hash="old", created_at="a"))
        store.papers.insert_extraction(id_, "old", "v", {"n": 1}, "z")
        store.papers.insert_extraction(id_, "new", "v", {"n": 2}, "a")
        store.papers.save_fulltext(FullTextState(id_, "parsed", "sha", "doc"), "t")
    add_note(store, "note")
    deleted = add_note(store, "deleted")
    store.reader.delete_annotation(deleted.id, "t2")
    add_inbox(store, "inbox", "done")
    add_inbox(store, "pending", "pending")
    loaded = store.search.snapshot("astronomy")
    assert {item.paper_id for item in loaded.candidates} == set(ids)
    assert tuple(item.id for item in loaded.embeddings.papers) == ("inbox", "note", "read")
    assert set(loaded.embeddings.fulltexts) == {"inbox", "note", "read"}
    assert all(item.data == {"n": 2} for item in loaded.embeddings.extractions)
    assert len(loaded.embeddings.extractions) == 3 and len(loaded.embeddings.embeddings) == 6


def test_current_fulltext_pointer_and_original_pages_are_preserved(store):
    _paper(store, "P", "Unrelated")
    store.papers.save_fulltext(FullTextState("P", "parsed", "sha", "old"), "t")
    store.search.replace_document("P", "old", (Block("text", "Straße", 8),))
    assert [(item.page, item.text) for item in store.search.snapshot("strasse").candidates] == [
        (8, "Straße")
    ]
    store.papers.save_fulltext(FullTextState("P", "parsed", "sha2", "new"), "t2")
    assert store.search.snapshot("strasse").candidates == ()
    store.search.replace_document("P", "new", (Block("text", "Straße", 9),))
    assert store.search.snapshot("strasse").candidates[0].page == 9


@pytest.mark.parametrize(
    ("query", "terms", "matched"),
    [
        ("ｘ", ("x",), True),
        ("x%", ("x",), True),
        ("x_", ("x_",), False),
        ('Astronomy OR "absent"', ("astronomy", "or", "absent"), False),
        ('Astronomy") OR ("absent', ("astronomy", "or", "absent"), False),
        ("x astronomy x", ("x", "astronomy"), True),
        ("! % \\", (), False),
    ],
)
def test_normalized_short_and_long_terms_remain_literal_and_match_with_and(
    store, query, terms, matched
):
    _paper(store, "P", "ＡＳＴＲＯＮＯＭＹ x")
    loaded = store.search.snapshot(query)
    assert loaded.terms == terms and bool(loaded.candidates) is matched
    if terms and all(len(term) < 3 for term in terms):
        assert all(item.bm25 == 0.0 for item in loaded.candidates)


def test_empty_terms_still_take_five_selects_and_keep_valid_embedding_snapshot(store):
    _paper(store, "P")
    store.embeddings.add(record())
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)

    event.listen(store._db._engine, "after_cursor_execute", capture)
    try:
        loaded = store.search.snapshot("!!!")
    finally:
        event.remove(store._db._engine, "after_cursor_execute", capture)
    assert len([sql for sql in statements if sql.startswith("SELECT")]) == 5
    assert loaded.candidates == loaded.terms == () and not loaded.embedding_problem
    assert loaded.embeddings.embeddings == (record(),)


@pytest.mark.parametrize("authors", ["{", "null", "42"])
def test_bad_embedding_projection_keeps_complete_lexical_results(store, authors):
    _paper(store, "P", "Ｓｔｒａße")
    store._db.write(
        text("UPDATE paper SET authors=:authors WHERE id='P'").bindparams(authors=authors)
    )
    loaded = store.search.snapshot("strasse")
    assert loaded.embedding_problem is True and loaded.embeddings is None
    assert len(loaded.candidates) == 1 and loaded.candidates[0].title == "Ｓｔｒａße"
    assert loaded.candidates[0].bm25 < 0


@pytest.mark.parametrize("failed_select", [1, 2, 3, 4, 5])
def test_database_failures_propagate_and_release_transaction(store, failed_select):
    _paper(store, "P")
    count = 0

    def fail(_conn, _cursor, sql, _params, _context, _many):
        nonlocal count
        if sql.startswith("SELECT"):
            count += 1
            if count == failed_select:
                raise sqlite3.OperationalError("injected database failure")

    event.listen(store._db._engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(OperationalError, match="injected"):
            store.search.snapshot("astronomy")
    finally:
        event.remove(store._db._engine, "before_cursor_execute", fail)
    assert store._db._active is None
    writer = SqlStore(str(store._db._engine.url))
    try:
        writer.papers.fill_missing("P", {"abstract": "Writer succeeds"})
        assert store.search.snapshot("astronomy").embeddings.papers[0].abstract == "Writer succeeds"
    finally:
        writer.dispose()


def test_concurrent_write_cannot_mix_lexical_and_embedding_snapshots(store, monkeypatch):
    _paper(store, "P", "oldmatch")
    _paper(store, "Q", "oldmatch", library=False)
    store.embeddings.add(record())
    writer = SqlStore(str(store._db._engine.url))
    started, futures = Event(), []
    convert = embedding_repo._snapshot

    def change():
        started.set()
        with writer.atomic():
            writer._db.write(text("UPDATE paper SET title='newmatch' WHERE id='P'"))
            writer.reader.set_state("Q", "read", None, "t2")
            writer.embeddings.add(record(input_hash="new"))

    def after_transaction(rows):
        assert store._db._active is None
        futures[0].result(timeout=5)
        return convert(rows)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:

            def capture(_conn, _cursor, sql, _params, _context, _many):
                if sql.startswith("SELECT") and not futures:
                    futures.append(executor.submit(change))
                    assert started.wait(5) and not futures[0].done()

            monkeypatch.setattr(embedding_repo, "_snapshot", after_transaction)
            event.listen(store._db._engine, "after_cursor_execute", capture)
            try:
                loaded = store.search.snapshot("oldmatch")
            finally:
                event.remove(store._db._engine, "after_cursor_execute", capture)
        assert tuple(item.title for item in loaded.candidates) == ("oldmatch", "oldmatch")
        assert [(item.id, item.title) for item in loaded.embeddings.papers] == [("P", "oldmatch")]
        assert len(loaded.embeddings.embeddings) == 1
        updated = store.search.snapshot("oldmatch")
        assert tuple(item.paper_id for item in updated.candidates) == ("Q",)
        assert tuple(item.id for item in updated.embeddings.papers) == ("P", "Q")
        assert len(updated.embeddings.embeddings) == 2
    finally:
        writer.dispose()
