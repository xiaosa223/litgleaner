"""过期诊断全历史快照保持固定查询数、一致事务和只读边界。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Event

import pytest
from sqlalchemy import event, update
from tests.integration.test_export_repo import answer, paper, question
from tests.integration.test_outline_repo import result as outline_result
from tests.integration.test_topic_export_repo import add_assessment, add_score
from tests.integration.test_topic_map_repo import result as map_result

from litgleaner.domain.identity import Alias
from litgleaner.infra.db import stale_repo
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import job
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.stale import StaleSnapshot
from litgleaner.ports.store import FullTextState, JobSpec, TopicState, TriageResult
from litgleaner.ports.translations import TranslationKey


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'stale.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    yield store
    store.dispose()


def test_empty_snapshot_has_exact_frozen_contract(store):
    loaded = store.stale_snapshot()
    assert loaded == StaleSnapshot((), {}, {}, {}, (), (), (), (), (), (), {}, (), (), (), ())
    with pytest.raises(FrozenInstanceError):
        loaded.papers = ()


def test_all_papers_and_optional_metadata_are_preserved_in_stable_order(store):
    for id_ in ("Z", "A", "M"):
        store.papers.insert(paper(id_))
    store.papers.add_aliases("A", (Alias("z", "last"), Alias("a", "second"), Alias("a", "first")))
    store.papers.add_aliases("Z", (Alias("id", "z"),))
    fulltext = FullTextState("A", "parsed", "sha", "doc", "provider", "license", "good")
    store.papers.save_fulltext(fulltext, "t")
    store.reader.set_state("Z", "read", {"page": 3, "view": "pdf"}, "t")
    loaded = store.stale_snapshot()
    assert loaded.papers == tuple(paper(id_) for id_ in ("A", "M", "Z"))
    assert loaded.aliases == {
        "A": (Alias("a", "first"), Alias("a", "second"), Alias("z", "last")),
        "M": (),
        "Z": (Alias("id", "z"),),
    }
    assert loaded.fulltexts == {"A": fulltext}
    assert loaded.reading == {"Z": store.reader.get_state("Z")}
    assert loaded == store.stale_snapshot()


def test_all_extraction_assessment_and_policy_history_retains_response_and_prompt_fields(store):
    store.papers.insert(paper("P"))
    ids = []
    for n in range(30):
        store.papers.insert_extraction("P", f"input-{n}", f"v-{n}", {"n": n}, f"t-{99 - n}")
        ids.append(
            add_assessment(
                store,
                "P",
                input_hash=f"input-{n}",
                model=f"response-{n}",
                prompt_version=f"prompt-{n}",
            )
        )
    add_score(store, ids[0], policy="z-policy", value=0, provisional=True)
    add_score(store, ids[0], policy="a-policy", value=90)
    add_score(store, ids[1], policy="a-policy", value=70)
    loaded = store.stale_snapshot()
    assert len(loaded.extractions) == len(loaded.assessments) == 30
    assert tuple(item.data["n"] for item in loaded.extractions) == tuple(range(30))
    assert loaded.extractions[-1] == store.papers.get_extraction("P")
    assert tuple(item.record.id for item in loaded.assessments) == tuple(ids)
    for n, item in enumerate(loaded.assessments):
        assert item.record == store.judgments.get_assessment(ids[n])
        assert (item.model, item.prompt_version, item.created_at) == (
            f"response-{n}",
            f"prompt-{n}",
            "t",
        )
        assert item.record.goal_links[0].verified is True
    assert tuple((item.assessment.id, item.policy_hash) for item in loaded.scores) == (
        (ids[0], "a-policy"),
        (ids[0], "z-policy"),
        (ids[1], "a-policy"),
    )
    assert loaded.scores[0].assessment is loaded.assessments[0].record
    assert loaded.scores[1].score == 0 and loaded.scores[1].provisional is True
    assert loaded.scores[1].undetermined == ("unclear",)
    with pytest.raises(FrozenInstanceError):
        loaded.assessments[0].prompt_version = "changed"


def test_translation_versions_and_nullable_paper_identity_remain_complete(store):
    store.papers.insert(paper("P"))
    for n in reversed(range(30)):
        key = TranslationKey("block", "source", "zh-CN", f"glossary-{n:02}", f"version-{n}")
        store.translations.put(key, (f"译文-{n}",), f"response-{n}", "P", f"t-{n}")
    detached = TranslationKey("block", "detached", "en", "glossary", "old-version")
    store.translations.put(detached, ("translation",), "raw-response", None, "old-time")
    loaded = store.stale_snapshot()
    assert len(loaded.translations) == 31
    assert all(item.cache == store.translations.get(item.cache.key) for item in loaded.translations)
    orphan = next(item for item in loaded.translations if item.cache.key == detached)
    assert orphan.paper_id is None and orphan.cache.model == "raw-response"
    attached = [item for item in loaded.translations if item.paper_id == "P"]
    assert tuple(item.cache.key.glossary_hash for item in attached) == tuple(
        f"glossary-{n:02}" for n in range(30)
    )
    with pytest.raises(FrozenInstanceError):
        orphan.paper_id = "P"


def test_qa_has_no_default_fifty_window_and_preserves_response_producer_and_input(store):
    store.papers.insert(paper("P"))
    for n in reversed(range(55)):
        item = question("P", n, "same-time")
        store.qa.add_question(item)
        store.qa.add_answer(answer(item, "same-time"))
    loaded = store.stale_snapshot()
    assert len(loaded.questions) == len(loaded.answers) == 55
    assert tuple(item.id for item in loaded.questions) == tuple(f"P-q-{n:03}" for n in range(55))
    for item in loaded.answers:
        assert item == store.qa.answer(item.question_id)
        assert item.model == "test-model" and item.producer_version == "producer-v1"
        assert item.input_hash == f"input-{item.question_id}"
        assert item.statements[1].citations[0].page == 4
    assert loaded == store.stale_snapshot()


def test_ask_jobs_only_include_valid_ask_payloads_and_duplicate_question_uses_stable_key(store):
    old = {"question_id": "Q", "doc_key": "old", "chunker": "v1"}
    chosen = {"question_id": "Q", "doc_key": "new", "document_hash": "hash", "chunker": "v2"}
    for key, stage, payload in (
        ("ask:a", "ask", old),
        ("ask:z", "ask", chosen),
        ("ignored", "extract", {"question_id": "Q", "doc_key": "wrong-stage"}),
        ("missing", "ask", {}),
        ("boolean", "ask", {"question_id": True}),
        ("empty", "ask", {"question_id": " "}),
        ("array", "ask", {}),
        ("malformed", "ask", {}),
    ):
        store.jobs.enqueue(JobSpec(key, stage, payload), "t")
    store.jobs.mark_done("ask:z", "t")
    store._db.write(update(job).where(job.c.key == "array").values(payload="[]"))
    store._db.write(update(job).where(job.c.key == "malformed").values(payload="broken"))
    assert store.stale_snapshot().ask_jobs == {"Q": chosen}


def test_topic_hash_triage_and_result_histories_are_not_scoped_or_truncated(store):
    store.papers.insert(paper("P"))
    for topic in ("Z", "A"):
        store.judgments.save_topic_state(TopicState(topic, "active", "new", "old-date"), "t")
        for hash_ in ("old", "new"):
            store.judgments.save_triage(
                TriageResult("P", topic, hash_, hash_ == "new", "why", "triage-v", "response"), "t"
            )
    maps, outlines = [], []
    for n in range(26):
        maps.append(store.topic_maps.add(map_result(f"map-{n:02}", input_hash=f"input-{n}")))
        outlines.append(
            store.topic_outlines.add(outline_result(f"outline-{n:02}", input_hash=f"input-{n}"))
        )
    loaded = store.stale_snapshot()
    assert tuple(item.topic_id for item in loaded.topic_states) == ("A", "Z")
    assert tuple((item.topic_id, item.topic_hash) for item in loaded.triages) == (
        ("A", "new"),
        ("A", "old"),
        ("Z", "new"),
        ("Z", "old"),
    )
    assert all(
        item.model == "response" and item.prompt_version == "triage-v" for item in loaded.triages
    )
    assert loaded.maps == tuple(maps) and loaded.outlines == tuple(outlines)


@pytest.mark.parametrize("count", [0, 1000])
def test_fourteen_selects_on_one_transaction_are_parameter_bounded_and_never_write(
    store, count, monkeypatch
):
    with store.atomic():
        for n in reversed(range(count)):
            id_ = f"P{n:04}"
            store.papers.insert(paper(id_))
            store.papers.add_aliases(id_, (Alias("id", id_),))
            store.papers.insert_extraction(id_, "input", "v", {}, "t")
    with store._db._engine.connect() as connection:
        connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    statements, connections = [], set()
    convert = stale_repo._snapshot

    def capture(conn, _cursor, sql, params, _context, _many):
        statements.append((sql, params))
        connections.add(id(conn))
        if sql.startswith("SELECT"):
            assert store._db._active is conn

    def outside_transaction(rows):
        assert store._db._active is None
        return convert(rows)

    def reject(_statement):
        pytest.fail("stale snapshot must not write")

    monkeypatch.setattr(store._db, "write", reject)
    monkeypatch.setattr(stale_repo, "_snapshot", outside_transaction)
    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        loaded = store.stale_snapshot()
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    selects = [(sql, params) for sql, params in statements if sql.startswith("SELECT")]
    assert len(selects) == 14 and len(connections) == 1
    assert all(len(params) <= 1 and "LIMIT" not in sql for sql, params in selects)
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql, _params in statements)
    assert tuple(item.id for item in loaded.papers) == tuple(f"P{n:04}" for n in range(count))
    assert len(loaded.extractions) == len(loaded.aliases) == count


def test_snapshot_cannot_mix_old_papers_with_concurrently_updated_reading(store):
    original = paper("P")
    store.papers.insert(original)
    writer = SqlStore(str(store._db._engine.url))
    started, futures = Event(), []

    def change():
        started.set()
        with writer.atomic():
            writer.papers.fill_missing("P", {"abstract": "New abstract"})
            writer.reader.set_state("P", "read", {"page": 4}, "t2")

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:

            def capture(_conn, _cursor, sql, _params, _context, _many):
                if sql.startswith("SELECT") and not futures:
                    futures.append(executor.submit(change))
                    assert started.wait(5)

            event.listen(store._db._engine, "after_cursor_execute", capture)
            try:
                loaded = store.stale_snapshot()
            finally:
                event.remove(store._db._engine, "after_cursor_execute", capture)
            assert loaded.papers == (original,) and loaded.reading == {}
            futures[0].result(timeout=5)
        updated = store.stale_snapshot()
        assert (
            updated.papers[0].abstract == "New abstract" and updated.reading["P"].status == "read"
        )
    finally:
        writer.dispose()
