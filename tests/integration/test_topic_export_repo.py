"""课题导出按当前画像和评价指纹做六次只读查询，保留未评分候选。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Event

import pytest
from sqlalchemy import event, text

from litgleaner.domain.goal_links import GoalLink
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import Paper
from litgleaner.domain.rubric import ScoreResult
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.export import TopicExportQuery, TopicExportSnapshot
from litgleaner.ports.store import (
    DigestItem,
    DigestRecord,
    FullTextState,
    NewAssessment,
    TriageResult,
)

_QUERY = TopicExportQuery("topic", "current", "semantic", "policy")


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'topic-export.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    yield store
    store.dispose()


def add_paper(store, id_, *, topic="topic", topic_hash="current", relevant=True, triaged=True):
    item = Paper(id_, f"Original {id_}", id_, None, ("A. Author",), None, None, None, "2000-01-01")
    store.papers.insert(item)
    if triaged:
        store.judgments.save_triage(
            TriageResult(id_, topic, topic_hash, relevant, "why", "v", "model"), "t"
        )
    return item


def add_assessment(store, paper_id, **changes):
    item = NewAssessment(
        paper_id,
        "topic",
        "current",
        "semantic",
        "input",
        {"novelty": 5},
        {"novelty": {"quote": "Original evidence", "verified": True}},
        True,
        "model",
        "v",
        (
            GoalLink(
                goal_id="g",
                reuse="Model recommendation",
                quote="Original evidence",
                page=0,
                verified=True,
            ),
        ),
    )
    store.judgments.insert_assessment(replace(item, **changes), "t")
    return store._db.fetch(text("SELECT max(id) FROM assessment"))[0][0]


def add_score(store, assessment_id, *, policy="policy", value=85, provisional=False):
    store.judgments.save_score(
        assessment_id,
        policy,
        ScoreResult(value, "must_read", provisional, False, ("unclear",)),
        "t",
    )


def test_empty_scope_has_complete_empty_snapshot_and_frozen_dtos(store):
    snapshot = store.topic_export_snapshot(_QUERY)
    assert snapshot == TopicExportSnapshot((), {}, {}, {}, {}, {})
    with pytest.raises(FrozenInstanceError):
        snapshot.papers = ()
    with pytest.raises(FrozenInstanceError):
        _QUERY.topic_id = "changed"


def test_candidates_are_all_current_relevant_papers_without_library_delivery_or_date_filters(store):
    expected = [add_paper(store, "Z"), add_paper(store, "A")]
    for id_, changes in (
        ("old", {"topic_hash": "old"}),
        ("other", {"topic": "other"}),
        ("rejected", {"relevant": False}),
        ("untriaged", {"triaged": False}),
    ):
        add_paper(store, id_, **changes)
    store.outputs.create_digest(
        DigestRecord("D", "route", "topic", "channel", "2000-01"),
        (DigestItem("Z", False, {}),),
        "t",
    )
    store.outputs.set_delivery("D", "channel", "sent", "t")
    loaded = store.topic_export_snapshot(_QUERY)
    assert loaded.papers == tuple(sorted(expected, key=lambda p: p.id))
    assert loaded.aliases == {"A": (), "Z": ()}
    assert loaded.scores == loaded.extractions == loaded.reading == loaded.fulltexts == {}
    assert store.topic_export_snapshot(replace(_QUERY, topic_id="other")).papers == (
        store.papers.get("other"),
    )
    assert store.topic_export_snapshot(replace(_QUERY, topic_hash="old")).papers == (
        store.papers.get("old"),
    )
    assert store.topic_export_snapshot(replace(_QUERY, topic_id="topic' OR 1=1 --")).papers == ()


def test_aliases_latest_extraction_fulltext_and_reading_are_scoped_and_stable(store):
    for id_, topic in (("B", "topic"), ("A", "topic"), ("foreign", "other")):
        add_paper(store, id_, topic=topic)
        store.papers.add_aliases(
            id_, (Alias("z", id_ + "-z"), Alias("a", id_ + "-b"), Alias("a", id_ + "-a"))
        )
        store.papers.insert_extraction(id_, "older", "v1", {"method": "old"}, "later-date")
        store.papers.insert_extraction(
            id_, "newest", "v2", {"method": {"text": id_, "verified": True}}, "earlier-date"
        )
        store.papers.save_fulltext(
            FullTextState(id_, "parsed", f"sha-{id_}", f"doc-{id_}", "provider", "license", "good"),
            "t",
        )
        store.reader.set_state(
            id_, "read" if id_ == "A" else "reading", {"page": 0, "view": "pdf"}, "t"
        )
    loaded = store.topic_export_snapshot(_QUERY)
    assert (
        tuple(loaded.aliases)
        == tuple(loaded.extractions)
        == tuple(loaded.fulltexts)
        == tuple(loaded.reading)
        == ("A", "B")
    )
    assert loaded.aliases["A"] == (Alias("a", "A-a"), Alias("a", "A-b"), Alias("z", "A-z"))
    for id_ in ("A", "B"):
        assert loaded.extractions[id_] == store.papers.get_extraction(id_)
        assert loaded.extractions[id_].extractor_version == "v2"
        assert loaded.fulltexts[id_] == store.papers.get_fulltext(id_)
        assert loaded.reading[id_] == store.reader.get_state(id_)
    assert store.topic_export_snapshot(_QUERY) == loaded


def test_latest_matching_assessment_requires_current_score_without_fallback(store):
    for id_ in ("unscored", "wrong-policy", "scored", "wrong-topic"):
        add_paper(store, id_)
    old = add_assessment(store, "unscored", input_hash="old")
    add_score(store, old, value=100)
    add_assessment(store, "unscored", input_hash="new")
    wrong_policy = add_assessment(store, "wrong-policy")
    add_score(store, wrong_policy, policy="old-policy")
    current = add_assessment(store, "scored")
    add_score(store, current, value=0, provisional=True)
    for changes in ({"topic_hash": "old"}, {"semantic_hash": "old"}, {"topic_id": "other"}):
        newer = add_assessment(store, "scored", **changes)
        add_score(store, newer, value=99)
    foreign = add_assessment(store, "wrong-topic", topic_id="other")
    add_score(store, foreign)
    loaded = store.topic_export_snapshot(_QUERY)
    assert tuple(loaded.scores) == ("scored",)
    found = loaded.scores["scored"]
    assert found.assessment == store.judgments.get_assessment(current)
    assert found.score == 0 and found.provisional is True and found.undetermined == ("unclear",)
    assert found.assessment.goal_links[0].verified is True
    assert store.topic_export_snapshot(replace(_QUERY, policy_hash="missing")).scores == {}
    changed = store.topic_export_snapshot(replace(_QUERY, semantic_hash="old"))
    assert changed.scores["scored"].score == 99


def test_missing_optional_records_never_drop_a_candidate(store):
    expected = add_paper(store, "missing")
    loaded = store.topic_export_snapshot(_QUERY)
    assert loaded.papers == (expected,) and loaded.aliases == {"missing": ()}
    assert loaded.extractions == loaded.scores == loaded.fulltexts == loaded.reading == {}


@pytest.mark.parametrize("count", [0, 10, 1000])
def test_six_selects_on_one_short_transaction_are_scoped_parameter_bounded_and_read_only(
    store, count, monkeypatch
):
    with store.atomic():
        for n in reversed(range(count)):
            id_ = f"P{n:04}"
            add_paper(store, id_)
            store.papers.add_aliases(id_, (Alias("id", id_),))
            store.papers.insert_extraction(id_, "input", "v", {}, "t")
        add_paper(store, "outside", topic="other")
    with store._db._engine.connect() as connection:
        connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    statements, connections = [], set()

    def capture(conn, _cursor, sql, params, _context, _many):
        statements.append((sql, params))
        connections.add(id(conn))

    def reject(_statement):
        pytest.fail("topic snapshot must not write")

    monkeypatch.setattr(store._db, "write", reject)
    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        loaded = store.topic_export_snapshot(_QUERY)
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    selects = [(sql, params) for sql, params in statements if sql.startswith("SELECT")]
    assert len(selects) == 6 and len(connections) == 1
    assert all(
        "triage" in sql and "topic_id" in sql and "topic_hash" in sql for sql, _params in selects
    )
    assert all(len(params) <= 7 for _sql, params in selects)
    assert all(sql.startswith(("SELECT", "BEGIN")) for sql, _params in statements)
    assert tuple(item.id for item in loaded.papers) == tuple(f"P{n:04}" for n in range(count))
    assert len(loaded.extractions) == len(loaded.aliases) == count and store._db._active is None


def test_snapshot_does_not_mix_old_candidate_metadata_with_a_concurrent_new_reading_state(store):
    add_paper(store, "A")
    writer = SqlStore(str(store._db._engine.url))
    started, futures = Event(), []

    def change():
        started.set()
        with writer.atomic():
            writer.papers.fill_missing("A", {"abstract": "New abstract"})
            writer.reader.set_state("A", "read", {"page": 4}, "t2")

    with ThreadPoolExecutor(max_workers=1) as executor:

        def capture(_conn, _cursor, sql, _params, _context, _many):
            if sql.startswith("SELECT") and not futures:
                futures.append(executor.submit(change))
                assert started.wait(5)

        event.listen(store._db._engine, "after_cursor_execute", capture)
        try:
            loaded = store.topic_export_snapshot(_QUERY)
        finally:
            event.remove(store._db._engine, "after_cursor_execute", capture)
        assert loaded.papers[0].abstract is None and loaded.reading == {}
        futures[0].result(timeout=5)
    writer.dispose()
    updated = store.topic_export_snapshot(_QUERY)
    assert updated.papers[0].abstract == "New abstract" and updated.reading["A"].status == "read"
