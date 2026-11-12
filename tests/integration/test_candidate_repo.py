"""候选批量快照限定文库，保留原始 BM25，始终十次只读查询。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Event

import pytest
from sqlalchemy import event, text
from tests.integration.test_embedding_repo import record
from tests.integration.test_export_repo import paper

from litgleaner.domain.identity import Alias
from litgleaner.infra.db import candidate_repo
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import FullTextState


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'candidates.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    yield store
    store.dispose()


def add(store, id_, title="neural", *, abstract=None, library=True):
    item = replace(paper(id_), title=title, abstract=abstract)
    store.papers.insert(item)
    if library:
        store.reader.set_state(id_, "queued", None, "t")
    return item


def index(store, paper_id, kind="paper", id_="custom", *, version="", page=None, body="neural"):
    store._db.write(
        text(
            "INSERT INTO memory_fts(kind,id,paper_id,page,authorship,source_version,title,text) "
            "VALUES (:kind,:id,:paper,:page,'author',:version,'',:body)"
        ).bindparams(kind=kind, id=id_, paper=paper_id, page=page, version=version, body=body)
    )


def require_outside_conversion(store, monkeypatch):
    for name in ("_snapshot", "_embedding", "_text_hits"):
        convert = getattr(candidate_repo, name)

        def checked(value, convert=convert):
            assert store._db._active is None
            return convert(value)

        monkeypatch.setattr(candidate_repo, name, checked)


def test_empty_and_missing_source_still_have_frozen_empty_candidate_contract(store):
    loaded = store.memory_candidates_snapshot("missing")
    assert loaded.facts.papers == loaded.embeddings == loaded.terms == loaded.text_hits == ()
    with pytest.raises(FrozenInstanceError):
        loaded.terms = ("changed",)


@pytest.mark.parametrize("count", [0, 1, 305])
def test_ten_selects_one_connection_no_writes_full_library_and_outside_conversion(
    store, count, monkeypatch
):
    with store.atomic():
        for number in reversed(range(count)):
            id_ = f"P{number:04}"
            add(store, id_)
            store.embeddings.add(record(id_))
    with store._db._engine.connect() as connection:
        connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    statements, connections = [], set()
    require_outside_conversion(store, monkeypatch)

    def captured(conn, _cursor, sql, params, _context, _many):
        statements.append((sql, params))
        connections.add(id(conn))
        if sql.startswith("SELECT"):
            assert store._db._active is conn

    monkeypatch.setattr(store._db, "write", lambda _: pytest.fail("snapshot wrote database"))
    event.listen(store._db._engine, "before_cursor_execute", captured)
    try:
        loaded = store.memory_candidates_snapshot("P0000")
    finally:
        event.remove(store._db._engine, "before_cursor_execute", captured)
    assert len([sql for sql, _ in statements if sql.startswith("SELECT")]) == 10
    assert len(connections) == 1 and store._db._active is None
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql, _ in statements)
    assert tuple(p.id for p in loaded.facts.papers) == tuple(f"P{i:04}" for i in range(count))
    assert len(loaded.embeddings) == count and len(loaded.text_hits) == max(0, count - 1)


def test_source_and_targets_are_limited_to_library_but_aliases_keep_global_identity(store):
    add(store, "A")
    add(store, "B")
    add(store, "outside", library=False)
    store.papers.add_aliases("outside", (Alias("doi", "10.1000/global"),))
    store.embeddings.add(record("outside"))
    loaded = store.memory_candidates_snapshot("A")
    assert {hit.paper_id for hit in loaded.text_hits} == {"B"}
    assert loaded.embeddings == ()
    assert loaded.facts.aliases["outside"] == (Alias("doi", "10.1000/global"),)
    missing = store.memory_candidates_snapshot("outside")
    assert missing.terms == missing.text_hits == ()


@pytest.mark.parametrize("kind", ["note", "highlight", "qa", "concept", "document"])
def test_personal_or_non_content_index_rows_never_supply_paper_text_signals(store, kind):
    add(store, "A")
    add(store, "B", "planetary")
    index(store, "B", kind)
    assert store.memory_candidates_snapshot("A").text_hits == ()


def test_existing_fulltext_must_match_current_pointer_and_preserve_position(store):
    add(store, "A")
    add(store, "B", "planetary")
    store.papers.save_fulltext(FullTextState("B", "parsed", doc_key="current"), "t")
    index(store, "B", "fulltext", "old", version="old", page=1)
    index(store, "B", "fulltext", "new", version="current", page=7)
    (hit,) = store.memory_candidates_snapshot("A").text_hits
    assert (hit.paper_id, hit.kind, hit.source_id, hit.page, hit.source_version) == (
        "B",
        "fulltext",
        "new",
        7,
        "current",
    )
    assert hit.bm25 < 0
    with pytest.raises(FrozenInstanceError):
        hit.bm25 = 0
    store.papers.save_fulltext(FullTextState("B", "parsed", doc_key="another"), "t")
    assert store.memory_candidates_snapshot("A").text_hits == ()


def test_extraction_index_is_latest_by_id_even_when_old_index_rows_survive(store):
    add(store, "A")
    add(store, "B", "planetary")
    store.papers.insert_extraction("B", "old", "v1", {"problem": "neural"}, "t9")
    old = store.papers.get_extraction("B")
    store.papers.insert_extraction("B", "latest", "v2", {"problem": "planetary"}, "t1")
    index(store, "B", "extraction", str(old.id))
    assert store.memory_candidates_snapshot("A").text_hits == ()
    store.papers.insert_extraction("B", "unverified", "v3", {"problem": "neural"}, "t0")
    latest = store.papers.get_extraction("B")
    (hit,) = store.memory_candidates_snapshot("A").text_hits
    assert hit.source_id == str(latest.id) and hit.source_version == ""
    assert store.memory_candidates_snapshot("A").facts.extractions == (latest,)


def test_each_source_kind_keeps_only_strongest_hit_without_losing_other_papers(store):
    add(store, "A")
    add(store, "B", "neural")
    add(store, "C", "neural")
    store.papers.save_fulltext(FullTextState("B", "parsed", doc_key="current"), "t")
    index(store, "B", "fulltext", "weak", version="current", body="neural " + "other " * 80)
    index(store, "B", "fulltext", "strong", version="current", body="neural neural neural")
    loaded = store.memory_candidates_snapshot("A")
    assert {(hit.paper_id, hit.kind) for hit in loaded.text_hits} == {
        ("B", "paper"),
        ("B", "fulltext"),
        ("C", "paper"),
    }
    assert next(hit for hit in loaded.text_hits if hit.kind == "fulltext").source_id == "strong"


@pytest.mark.parametrize("valid_metadata", [False, True])
def test_wrong_metadata_id_cannot_supply_or_hide_a_legitimate_paper_hit(store, valid_metadata):
    add(store, "A")
    add(store, "B", "neural" if valid_metadata else "planetary", abstract="other " * 200)
    index(store, "B", "paper", "wrong-id", body="neural " * 10)
    ranked = store._db.fetch(
        text(
            "SELECT id, bm25(memory_fts,0,0,0,0,0,0,4,1) AS rank FROM memory_fts "
            "WHERE memory_fts MATCH :query AND kind='paper' AND paper_id='B' ORDER BY rank"
        ).bindparams(query='"neural"')
    )
    assert ranked[0].id == "wrong-id"
    assert len(ranked) == (2 if valid_metadata else 1)
    loaded = store.memory_candidates_snapshot("A")
    assert tuple(hit.source_id for hit in loaded.text_hits) == (("B",) if valid_metadata else ())


@pytest.mark.parametrize("title", ['neural OR ("*)', "Ｎeural", "神经网络模型"])
def test_or_recall_uses_literal_normalized_terms_and_supports_cjk(store, title):
    add(store, "A", title)
    add(store, "B", "neural 神经网络模型")
    add(store, "C", "planetary")
    loaded = store.memory_candidates_snapshot("A")
    assert {hit.paper_id for hit in loaded.text_hits} == {"B"}
    assert loaded.terms


def test_abstract_is_used_for_or_recall_and_terms_limit_is_applied(store):
    add(store, "A", "neural", abstract="spectral quasar")
    add(store, "B", "spectral")
    add(store, "C", "quasar")
    assert {hit.paper_id for hit in store.memory_candidates_snapshot("A").text_hits} == {"B", "C"}
    assert store.memory_candidates_snapshot("A", terms_limit=1).text_hits == ()


def test_short_or_empty_terms_do_not_issue_a_wildcard_query(store):
    add(store, "A", "AI")
    add(store, "B", "AI")
    loaded = store.memory_candidates_snapshot("A")
    assert loaded.terms == loaded.text_hits == ()


def test_candidate_vectors_keep_complete_joint_fingerprint_history(store):
    add(store, "A")
    original = record("A")
    changed_input = replace(original, input_hash="other-input")
    changed_model = replace(original, embedder_version="other-producer")
    for item in (changed_input, original, changed_model):
        store.embeddings.add(item)
    loaded = store.memory_candidates_snapshot("A")
    assert loaded.embeddings == tuple(
        sorted(
            (original, changed_input, changed_model),
            key=lambda r: (r.input_hash, r.embedder_version),
        )
    )


def test_last_query_failure_releases_transaction_and_next_snapshot_recovers(store, monkeypatch):
    add(store, "A")
    add(store, "B")
    fetch, calls = store._db.fetch, []

    def fail_last(statement):
        calls.append(statement)
        if len(calls) == 10:
            raise RuntimeError("failed text query")
        return fetch(statement)

    with monkeypatch.context() as patch:
        patch.setattr(store._db, "fetch", fail_last)
        with pytest.raises(RuntimeError, match="failed text query"):
            store.memory_candidates_snapshot("A")
    assert store._db._active is None
    assert {hit.paper_id for hit in store.memory_candidates_snapshot("A").text_hits} == {"B"}


def test_concurrent_scope_and_index_changes_cannot_mix_versions_in_snapshot(store):
    add(store, "A")
    add(store, "B")
    add(store, "C", "quasar", library=False)
    writer = SqlStore(str(store._db._engine.url))
    started, futures = Event(), []

    def change():
        started.set()
        with writer.atomic():
            writer.papers.fill_missing("A", {"abstract": "quasar"})
            writer.reader.set_state("C", "queued", None, "t2")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:

            def capture(_conn, _cursor, sql, _params, _context, _many):
                if sql.startswith("SELECT") and not futures:
                    futures.append(executor.submit(change))
                    assert started.wait(5)

            event.listen(store._db._engine, "after_cursor_execute", capture)
            try:
                before = store.memory_candidates_snapshot("A")
            finally:
                event.remove(store._db._engine, "after_cursor_execute", capture)
            futures[0].result(timeout=5)
        assert before.terms == ("neural",) and {h.paper_id for h in before.text_hits} == {"B"}
        after = store.memory_candidates_snapshot("A")
        assert after.terms == ("neural", "quasar")
        assert {h.paper_id for h in after.text_hits} == {"B", "C"}
    finally:
        writer.dispose()
