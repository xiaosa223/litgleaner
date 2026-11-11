"""地图首写、快照批量读取和论文集合校验。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, update
from sqlalchemy.exc import IntegrityError

from litgleaner.domain.paper import Paper
from litgleaner.domain.topic_map import MapGroup, TopicMap
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import topic_map
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import TriageResult


def result(map_id="map", topic_id="topic", paper_ids=("P",), **changes):
    values = {
        "id": map_id,
        "topic_id": topic_id,
        "topic_hash": "hash",
        "input_hash": "input",
        "producer_version": "producer",
        "model": "fake",
        "reading_language": "zh-CN",
        "token_budget": 12000,
        "created_at": "2026-11-11T01:00:00+00:00",
        "scope_id": "scope",
        "considered_paper_ids": paper_ids,
        "input_paper_ids": paper_ids,
        "source_hashes": dict.fromkeys(paper_ids, "source"),
        "groups": (MapGroup(name="Direction", description="Original", paper_ids=paper_ids),),
        "ungrouped": (),
        "warnings": ("Original warning",),
    }
    return TopicMap(**(values | changes))


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'map.db'}"
    upgrade_database(url)
    ctx = SqlStore(url)
    ctx.papers.insert(Paper("P", "Original", "original", None, (), None, None, None, "t"))
    yield ctx
    ctx.dispose()


def test_first_result_is_immutable_and_same_fingerprint_returns_original(store):
    first = result()
    assert store.topic_maps.add(first) == first
    later = result("different-id", model="other", warnings=("later",))
    assert store.topic_maps.add(later) == first
    assert store.topic_maps.get("different-id") is None
    assert store.topic_maps.get(first.id) == first
    assert store.topic_maps.by_fingerprint("topic", "hash", "input", "producer") == first
    assert store.topic_maps.by_fingerprint("topic", "old", "input", "producer") is None


@pytest.mark.parametrize("change", ["topic_id", "topic_hash", "input_hash", "producer_version"])
def test_each_fingerprint_dimension_can_store_an_independent_result(store, change):
    first = store.topic_maps.add(result())
    second = store.topic_maps.add(result("second", **{change: "different"}))
    assert second != first and store.topic_maps.get("second") == second


def test_primary_key_cannot_be_reused_for_different_fingerprint(store):
    first = store.topic_maps.add(result())
    with pytest.raises(IntegrityError):
        store.topic_maps.add(result(input_hash="different"))
    assert store.topic_maps.get(first.id) == first


@pytest.mark.parametrize("statement", [update(topic_map).values(data="{}"), delete(topic_map)])
def test_database_rejects_rewriting_or_deleting_result(store, statement):
    first = store.topic_maps.add(result())
    with pytest.raises(IntegrityError, match="topic_map_immutable"):
        store._db.write(statement)
    assert store.topic_maps.get(first.id) == first


def test_history_and_latest_are_topic_scoped_and_stably_sorted_across_hashes(store):
    first = store.topic_maps.add(result("a"))
    later = store.topic_maps.add(result("b", topic_hash="new-hash"))
    newest = store.topic_maps.add(
        result("c", input_hash="new", created_at="2026-11-12T01:00:00+00:00")
    )
    store.topic_maps.add(result("foreign", "other", created_at="2026-11-13T01:00:00+00:00"))
    assert store.topic_maps.history("topic") == (newest, later, first)
    assert store.topic_maps.history("topic", 2) == (newest, later)
    assert store.topic_maps.latest("topic") == newest
    assert store.topic_maps.latest("missing") is None and store.topic_maps.get("missing") is None


@pytest.mark.parametrize("limit", [0, -1, True, 1.0, "2"])
def test_history_rejects_invalid_limit(store, limit):
    with pytest.raises(ValueError, match="limit_invalid"):
        store.topic_maps.history("topic", limit)


@pytest.mark.parametrize(
    "field,value",
    [
        ("topic_id", "other"),
        ("topic_hash", "older"),
        ("producer_version", "other-model"),
        ("reading_language", "en"),
        ("token_budget", 1000),
    ],
)
def test_compatible_history_isolates_each_current_configuration_dimension(store, field, value):
    expected = store.topic_maps.add(result())
    store.topic_maps.add(result("incompatible", input_hash="other-input", **{field: value}))
    assert store.topic_maps.compatible("topic", "hash", "producer", "zh-CN", 12000) == (expected,)
    assert store.topic_maps.compatible("missing", "hash", "producer", "zh-CN", 12000) == ()


def test_compatible_history_keeps_all_matches_beyond_twenty_and_stably_sorts_ties(store):
    older = store.topic_maps.add(
        result("zz-older", input_hash="older-input", created_at="2026-11-01T01:00:00+00:00")
    )
    saved = tuple(
        store.topic_maps.add(result(f"map-{n:02}", input_hash=f"input-{n}")) for n in range(25)
    )
    assert len(store.topic_maps.history("topic")) == 20
    assert store.topic_maps.compatible("topic", "hash", "producer", "zh-CN", 12000) == (
        *reversed(saved),
        older,
    )


def test_compatible_history_recovers_old_language_after_more_than_twenty_newer_maps(store):
    old = store.topic_maps.add(result(created_at="2026-11-01T01:00:00+00:00"))
    for n in range(25):
        store.topic_maps.add(
            result(f"new-{n:02}", input_hash=f"new-input-{n}", reading_language="en")
        )
    assert old not in store.topic_maps.history("topic")
    assert store.topic_maps.latest("topic").reading_language == "en"
    assert store.topic_maps.compatible("topic", "hash", "producer", "zh-CN", 12000) == (old,)


@pytest.mark.parametrize("budget", [True, False, 12000.0, "12000", 0, 511, 64001])
def test_compatible_history_rejects_invalid_or_non_integer_budget(store, budget):
    with pytest.raises(ValidationError):
        store.topic_maps.compatible("topic", "hash", "producer", "zh-CN", budget)


def test_compatible_history_rejects_unknown_language(store):
    with pytest.raises(ValueError, match="language_invalid"):
        store.topic_maps.compatible("topic", "hash", "producer", "fr", 12000)


def test_all_considered_papers_must_exist_even_when_not_sent(store):
    item = result(considered_paper_ids=("P", "missing"), ungrouped=("missing",))
    with pytest.raises(ValueError, match="paper_missing"):
        store.topic_maps.add(item)
    assert store.topic_maps.history("topic") == ()


def test_model_copy_cannot_bypass_partition_validation_at_storage_boundary(store):
    broken = result().model_copy(update={"ungrouped": ("P",)})
    with pytest.raises(ValidationError, match="map_invalid_ungrouped"):
        store.topic_maps.add(broken)


@pytest.mark.parametrize("name", ["id", "topic_id", "input_hash", "producer_version", "scope_id"])
def test_blank_identifiers_do_not_create_untraceable_results(store, name):
    with pytest.raises(ValueError, match="identifier_empty"):
        store.topic_maps.add(result(**{name: " "}))


def test_concurrent_same_fingerprint_preserves_one_complete_original(tmp_path):
    url = f"sqlite:///{tmp_path / 'concurrent.db'}"
    upgrade_database(url)
    stores = [SqlStore(url), SqlStore(url)]
    stores[0].papers.insert(Paper("P", "Original", "original", None, (), None, None, None, "t"))
    barrier = Barrier(2)

    def create(index):
        barrier.wait()
        return stores[index].topic_maps.add(result(f"map-{index}", model=f"model-{index}"))

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            saved = list(executor.map(create, range(2)))
        assert saved[0] == saved[1]
        assert stores[0].topic_maps.history("topic") == (saved[0],)
        assert saved[0].model == f"model-{saved[0].id[-1]}"
    finally:
        for ctx in stores:
            ctx.dispose()


def test_snapshot_selects_current_relevant_papers_and_latest_extraction_without_writes(store):
    with store.atomic():
        for id_ in ("A", "B", "C", "D"):
            store.papers.insert(Paper(id_, id_, id_, None, (), None, None, None, "t"))
        for id_, topic, hash_, relevant in (
            ("P", "topic", "hash", True),
            ("A", "topic", "hash", True),
            ("B", "topic", "hash", False),
            ("C", "topic", "old", True),
            ("D", "other", "hash", True),
        ):
            store.judgments.save_triage(
                TriageResult(id_, topic, hash_, relevant, "", "v", "m"), "t"
            )
        store.papers.insert_extraction("P", "older", "v1", {"summary": "old"}, "later-date")
        store.papers.insert_extraction("P", "newer", "v2", {"summary": "latest"}, "earlier-date")
        store.papers.insert_extraction("B", "other", "v1", {"summary": "rejected"}, "t")
    before = store.stats()
    snapshot = store.topic_maps.snapshot("topic", "hash")
    assert tuple(paper.id for paper in snapshot.papers) == ("A", "P")
    assert set(snapshot.extractions) == {"P"}
    assert snapshot.extractions["P"] == store.papers.get_extraction("P")
    assert snapshot.extractions["P"].data == {"summary": "latest"}
    assert store.stats() == before
    assert store.topic_maps.snapshot("missing", "hash").papers == ()


@pytest.mark.parametrize("count", [10, 1000])
def test_snapshot_uses_two_selects_and_json_paper_validation_avoids_bind_limit(store, count):
    ids = tuple(f"P{n:04}" for n in range(count))
    with store.atomic():
        for id_ in reversed(ids):
            store.papers.insert(Paper(id_, id_, id_, None, (), None, None, None, "t"))
            store.judgments.save_triage(TriageResult(id_, "topic", "hash", True, "", "v", "m"), "t")
            store.papers.insert_extraction(id_, "old", "v", {"summary": id_}, "t")
    with store._db._engine.connect() as connection:
        driver = connection.connection.driver_connection
        driver.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    selects = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        snapshot = store.topic_maps.snapshot("topic", "hash")
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    assert len(selects) == 2
    assert tuple(paper.id for paper in snapshot.papers) == ids
    assert len(snapshot.extractions) == count
    assert store.topic_maps.add(result(paper_ids=ids)).considered_paper_ids == ids
