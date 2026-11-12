"""论文记忆读取全部文库事实，保留全局身份并限制为八次只读查询。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Event

import pytest
from sqlalchemy import event, insert
from tests.integration.test_citation_repo import reference
from tests.integration.test_export_repo import paper
from tests.integration.test_knowledge_repo import occurrence

from litgleaner.domain.identity import Alias
from litgleaner.domain.inbox import InboxItem
from litgleaner.domain.reading import NewAnnotation
from litgleaner.infra.db import memory_repo
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import concept
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.memory import MemorySnapshot
from litgleaner.ports.store import FullTextState


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'memory.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    yield store
    store.dispose()


def add_paper(store, paper_id, status=None):
    store.papers.insert(paper(paper_id))
    if status is not None:
        store.reader.set_state(paper_id, status, None, "t")


def add_inbox(store, paper_id, status):
    item = InboxItem(f"in-{paper_id}", "doi", "10.1000/test", "pending", "t")
    store.inbox.add(item)
    store.inbox.finish(item.id, status, paper_id, None, "t")


def add_note(store, paper_id):
    return store.reader.add_annotation(
        NewAnnotation(paper_id, "note", None, None, None, "用户原话"), "t"
    )


def test_empty_snapshot_has_exact_frozen_contract(store):
    loaded = store.memory_snapshot()
    assert loaded == MemorySnapshot((), {}, (), {}, (), (), (), {})
    assert loaded.concept_names == {}
    with pytest.raises(FrozenInstanceError):
        loaded.papers = ()


def test_legacy_snapshot_constructor_gives_independent_default_concept_names():
    first = MemorySnapshot((), {}, (), {}, (), (), (), {})
    second = MemorySnapshot((), {}, (), {}, (), (), (), {})
    assert first.concept_names == second.concept_names == {}
    assert first.concept_names is not second.concept_names


def test_library_scope_includes_all_reading_states_live_notes_and_done_inbox(store):
    expected = ("abandoned", "inbox", "note", "queued", "read", "reading")
    for status in ("abandoned", "queued", "read", "reading"):
        add_paper(store, status, status)
    for paper_id in ("inbox", "note", "candidate", "deleted", "pending", "failed"):
        add_paper(store, paper_id)
    add_note(store, "note")
    deleted = add_note(store, "deleted")
    store.reader.delete_annotation(deleted.id, "t2")
    for status, paper_id in (("done", "inbox"), ("pending", "pending"), ("failed", "failed")):
        add_inbox(store, paper_id, status)
    loaded = store.memory_snapshot()
    assert loaded.papers == tuple(paper(paper_id) for paper_id in expected)
    assert set(loaded.reading) == {"abandoned", "queued", "read", "reading"}
    assert loaded.reading == {key: store.reader.get_state(key) for key in loaded.reading}
    assert store.reader.get_state("inbox") is store.reader.get_state("note") is None


def test_aliases_keep_global_conflicts_and_do_not_narrow_to_library_scope(store):
    for paper_id in ("A", "B", "C", "D"):
        add_paper(store, paper_id, "read" if paper_id in {"A", "B"} else None)
    store.papers.add_aliases("A", (Alias("z", "last"), Alias("doi", "10.1000/shared")))
    store.papers.add_aliases("C", (Alias("id", "outside"), Alias("a", "first")))
    store.citations.add(
        reference(
            citing_paper_id="A",
            cited_aliases=(Alias("doi", "10.1000/shared"), Alias("id", "outside")),
        )
    )
    loaded = store.memory_snapshot()
    assert loaded.aliases == {
        "A": (Alias("doi", "10.1000/shared"), Alias("z", "last")),
        "B": (),
        "C": (Alias("a", "first"), Alias("id", "outside")),
    }
    assert tuple(item.id for item in loaded.papers) == ("A", "B")
    assert {
        key
        for key, aliases in loaded.aliases.items()
        if set(aliases).intersection(loaded.citations[0].cited_aliases)
    } == {"A", "C"}
    store.reader.set_state("C", "abandoned", None, "t")
    assert store.memory_snapshot().aliases == loaded.aliases


def test_aliases_are_present_even_when_no_paper_belongs_to_library(store):
    add_paper(store, "external")
    aliases = (Alias("doi", "10.1000/external"),)
    store.papers.add_aliases("external", aliases)
    loaded = store.memory_snapshot()
    assert loaded.papers == () and loaded.aliases == {"external": aliases}


def test_latest_extraction_is_by_id_not_time_or_validity_and_no_history_fallback(store):
    for paper_id in ("Z", "A", "candidate", "missing"):
        add_paper(store, paper_id, "queued" if paper_id != "candidate" else None)
    for paper_id in ("Z", "A", "candidate"):
        store.papers.insert_extraction(paper_id, "current", "v-current", {"valid": True}, "t9")
        store.papers.insert_extraction(paper_id, "old", "v-old", {}, "t1")
    loaded = store.memory_snapshot()
    assert tuple(item.paper_id for item in loaded.extractions) == ("A", "Z")
    assert loaded.extractions == tuple(store.papers.get_extraction(id_) for id_ in ("A", "Z"))
    assert all(item.data == {} and item.input_hash == "old" for item in loaded.extractions)


def test_fulltexts_and_reading_metadata_are_scoped_and_preserved(store):
    for paper_id in ("Z", "A", "candidate"):
        add_paper(store, paper_id, "reading" if paper_id == "A" else None)
        store.papers.save_fulltext(
            FullTextState(paper_id, "parsed", "sha", "doc", "provider", "license", "good"), "t"
        )
    add_note(store, "Z")
    store.reader.set_state("A", "reading", {"view": "pdf", "page": 0}, "t2")
    loaded = store.memory_snapshot()
    assert loaded.fulltexts == {key: store.papers.get_fulltext(key) for key in ("A", "Z")}
    assert loaded.reading["A"].position == {"view": "pdf", "page": 0}
    assert loaded.reading["A"].updated_at == "t2" and "Z" not in loaded.reading


def test_citation_sources_targets_aliases_and_original_evidence_survive_unchanged(store):
    for paper_id in ("A", "B", "external"):
        add_paper(store, paper_id, "read" if paper_id != "external" else None)
    items = (
        reference(citing_paper_id="A", cited_paper_id="external", page=0, bbox=(1, 2, 3, 4)),
        reference(citing_paper_id="A", position="api:2:h", source_position=2, influential=False),
        reference(citing_paper_id="A", position="other:0:h", source="other", influential=True),
        reference(citing_paper_id="B", cited_paper_id="A"),
        reference(citing_paper_id="external", cited_paper_id="A"),
    )
    for item in reversed(items):
        store.citations.add(item)
    loaded = store.memory_snapshot()
    assert loaded.citations == items[:-1]
    assert loaded.citations[0].cited_paper_id == "external"
    assert loaded.citations[1].cited_aliases == (Alias("doi", "10.1000/late"),)
    assert loaded.citations[0].raw_text == '  {"raw":"author\'s 引用"}\n  '


def test_all_occurrence_roles_and_provenance_are_retained_only_for_library(store):
    for paper_id in ("A", "B", "external"):
        add_paper(store, paper_id, "read" if paper_id != "external" else None)
    first = store.knowledge.register("Method", "en", "t")
    second = store.knowledge.register("Dataset", "en", "t")
    unused = store.knowledge.register("External only", "en", "t")
    items = (
        occurrence(first.id, paper_id="A", role="core"),
        occurrence(second.id, paper_id="A", role="prerequisite", verified=False, definition=None),
        occurrence(first.id, paper_id="B", role="mentioned"),
        occurrence(unused.id, paper_id="external"),
    )
    for item in reversed(items):
        store.knowledge.add_occurrence(item)
    loaded = store.memory_snapshot()
    assert loaded.concepts == tuple(sorted((first, second), key=lambda item: item.id))
    assert loaded.occurrences == tuple(sorted(items[:-1], key=lambda x: (x.paper_id, x.concept_id)))
    assert loaded == store.memory_snapshot()


def test_registered_concept_aliases_and_canonical_names_keep_eight_queries_and_unique_concepts(
    store,
):
    add_paper(store, "A", "read")
    add_paper(store, "B", "queued")
    add_paper(store, "external")
    shared = store.knowledge.register("Ｔransformer", "en", "t")
    store.knowledge.add_alias(shared.id, "变换器", "zh-CN", "t")
    store.knowledge.add_alias(shared.id, " Attention  Network ", "en", "t")
    external = store.knowledge.register("External only", "en", "t")
    store.knowledge.add_alias(external.id, "库外概念", "zh-CN", "t")
    store._db.write(insert(concept).values(id="no-alias", name=" Ｌegacy   Name ", created_at="t"))
    for item in (
        occurrence(shared.id, paper_id="A"),
        occurrence(shared.id, paper_id="B"),
        occurrence("no-alias", paper_id="A"),
        occurrence(external.id, paper_id="external"),
    ):
        store.knowledge.add_occurrence(item)
    statements = []

    def capture(_conn, _cursor, sql, _params, _context, _many):
        if sql.startswith("SELECT"):
            statements.append(sql)

    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        loaded = store.memory_snapshot()
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    assert len(statements) == 8 and "LEFT OUTER JOIN concept_alias" in statements[5]
    assert tuple(item.id for item in loaded.concepts) == tuple(sorted((shared.id, "no-alias")))
    assert loaded.concept_names == {
        shared.id: ("attention network", "transformer", "变换器"),
        "no-alias": ("legacy name",),
    }
    assert {item.concept_id for item in loaded.occurrences} == set(loaded.concept_names)


@pytest.mark.parametrize("count", [0, 55, 1100])
def test_fixed_eight_selects_one_connection_no_writes_or_parameter_limit(store, count, monkeypatch):
    with store.atomic():
        for n in reversed(range(count)):
            paper_id = f"P{n:04}"
            add_paper(store, paper_id, "queued")
            store.papers.add_aliases(paper_id, (Alias("id", paper_id),))
            store.papers.insert_extraction(paper_id, "input", "v", {}, "t")
    with store._db._engine.connect() as connection:
        connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    statements, connections = [], set()
    convert = memory_repo._snapshot

    def capture(conn, _cursor, sql, params, _context, _many):
        statements.append((sql, params))
        connections.add(id(conn))
        if sql.startswith("SELECT"):
            assert store._db._active is conn

    def outside_transaction(rows):
        assert store._db._active is None
        return convert(rows)

    def reject(_statement):
        pytest.fail("memory snapshot must not write")

    monkeypatch.setattr(store._db, "write", reject)
    monkeypatch.setattr(memory_repo, "_snapshot", outside_transaction)
    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        loaded = store.memory_snapshot()
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    selects = [(sql, params) for sql, params in statements if sql.startswith("SELECT")]
    assert len(selects) == 8 and len(connections) == 1
    assert all(len(params) <= 1 and "LIMIT" not in sql for sql, params in selects)
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql, _params in statements)
    assert tuple(item.id for item in loaded.papers) == tuple(f"P{n:04}" for n in range(count))
    assert len(loaded.extractions) == len(loaded.aliases) == len(loaded.reading) == count


def test_snapshot_cannot_mix_old_library_scope_with_concurrent_changes(store):
    add_paper(store, "P", "queued")
    add_paper(store, "Q")
    writer = SqlStore(str(store._db._engine.url))
    started, futures = Event(), []

    def change():
        started.set()
        with writer.atomic():
            writer.papers.fill_missing("P", {"abstract": "New abstract"})
            writer.reader.set_state("P", "read", {"page": 4}, "t2")
            writer.reader.set_state("Q", "queued", None, "t2")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:

            def capture(_conn, _cursor, sql, _params, _context, _many):
                if sql.startswith("SELECT") and not futures:
                    futures.append(executor.submit(change))
                    assert started.wait(5)

            event.listen(store._db._engine, "after_cursor_execute", capture)
            try:
                loaded = store.memory_snapshot()
            finally:
                event.remove(store._db._engine, "after_cursor_execute", capture)
            assert loaded.papers == (paper("P"),)
            assert loaded.reading["P"].status == "queued" and "Q" not in loaded.reading
            futures[0].result(timeout=5)
        updated = store.memory_snapshot()
        assert tuple(item.id for item in updated.papers) == ("P", "Q")
        assert updated.papers[0] == replace(paper("P"), abstract="New abstract")
        assert updated.reading["P"].status == "read"
    finally:
        writer.dispose()


def test_query_failure_releases_short_transaction_and_next_read_recovers(store, monkeypatch):
    add_paper(store, "P", "read")
    fetch, calls = store._db.fetch, []

    def fail_partway(statement):
        calls.append(statement)
        if len(calls) == 4:
            raise RuntimeError("failed query")
        return fetch(statement)

    with monkeypatch.context() as patch:
        patch.setattr(store._db, "fetch", fail_partway)
        with pytest.raises(RuntimeError, match="failed query"):
            store.memory_snapshot()
    assert store._db._active is None
    assert store.memory_snapshot().papers == (paper("P"),)
