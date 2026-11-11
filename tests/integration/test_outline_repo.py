"""综述结果首写完整附件、配置隔离与安全批量论文校验。"""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError

from litgleaner.domain.outline import OutlineGap, OutlineGroup, OutlinePaper, TopicOutline
from litgleaner.domain.paper import Paper
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import topic_outline
from litgleaner.infra.db.store import SqlStore


def result(outline_id="outline", topic_id="topic", paper_ids=("P",), **changes):
    values = {
        "id": outline_id,
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
        "goal_ids": ("goal",),
        "groups": (
            OutlineGroup(
                name="方向", description="原始分组", goal_ids=("goal",), paper_ids=paper_ids
            ),
        ),
        "gaps": (OutlineGap(description="覆盖不足候选", paper_ids=paper_ids),),
        "papers": tuple(
            OutlinePaper(
                paper_id=id_,
                title=f"标题 {id_}",
                table_markdown=f"| {id_} | 原始事实 |",
                bibtex=f"@article{{{id_},title={{原标题}}}}",
                csl_json='{"title":"原标题"}',
                needs_verification=True,
            )
            for id_ in paper_ids
        ),
        "ungrouped": (),
        "warnings": ("生成时警告",),
    }
    return TopicOutline(**(values | changes))


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'outline.db'}"
    upgrade_database(url)
    ctx = SqlStore(url)
    ctx.papers.insert(Paper("P", "Original", "original", None, (), None, None, None, "t"))
    yield ctx
    ctx.dispose()


def test_first_fingerprint_keeps_original_identity_and_complete_frozen_attachments(store):
    original = result()
    assert store.topic_outlines.add(original) == original
    changed = result("different-id", model="other", warnings=("later",), gaps=())
    changed = changed.model_copy(
        update={
            "papers": (
                changed.papers[0].model_copy(
                    update={"title": "修改标题", "table_markdown": "修改事实", "bibtex": "later"}
                ),
            )
        }
    )
    assert store.topic_outlines.add(changed) == original
    assert store.topic_outlines.get("different-id") is None
    assert store.topic_outlines.get(original.id) == original
    assert store.topic_outlines.by_fingerprint("topic", "hash", "input", "producer") == original
    assert store.topic_outlines.by_fingerprint("topic", "old", "input", "producer") is None


@pytest.mark.parametrize("field", ["topic_id", "topic_hash", "input_hash", "producer_version"])
def test_each_fingerprint_dimension_stores_independent_result(store, field):
    original = store.topic_outlines.add(result())
    other = store.topic_outlines.add(result("other", **{field: "different"}))
    assert other != original and store.topic_outlines.get("other") == other


def test_same_identity_with_new_fingerprint_cannot_replace_original(store):
    original = store.topic_outlines.add(result())
    with pytest.raises(IntegrityError):
        store.topic_outlines.add(result(input_hash="different"))
    assert store.topic_outlines.get(original.id) == original


@pytest.mark.parametrize(
    "statement", [update(topic_outline).values(data="{}"), delete(topic_outline)]
)
def test_raw_sql_update_and_delete_are_rejected(store, statement):
    original = store.topic_outlines.add(result())
    with pytest.raises(IntegrityError, match="topic_outline_immutable"):
        store._db.write(statement)
    assert store.topic_outlines.get(original.id) == original


@pytest.mark.parametrize("changes", [{"input_hash": "new"}, {"id": "replacement"}])
def test_raw_insert_or_replace_cannot_overwrite_identity_or_fingerprint(store, changes):
    original = store.topic_outlines.add(result())
    replacement = result(**changes)
    columns = {
        name: getattr(replacement, name)
        for name in ("id", "topic_id", "topic_hash", "input_hash", "producer_version", "created_at")
    }
    with pytest.raises(IntegrityError, match="topic_outline_immutable"):
        store._db.write(
            insert(topic_outline)
            .prefix_with("OR REPLACE")
            .values(**columns, data=replacement.model_dump_json())
        )
    assert store.topic_outlines.history("topic") == (original,)


def test_history_and_latest_are_topic_scoped_and_sort_time_then_identity(store):
    first = store.topic_outlines.add(result("a"))
    tied = store.topic_outlines.add(result("b", topic_hash="new-hash"))
    newest = store.topic_outlines.add(
        result("c", input_hash="new", created_at="2026-11-12T01:00:00+00:00")
    )
    store.topic_outlines.add(result("foreign", "other", created_at="2026-11-13T00:00:00+00:00"))
    assert store.topic_outlines.history("topic") == (newest, tied, first)
    assert store.topic_outlines.history("topic", 2) == (newest, tied)
    assert store.topic_outlines.latest("topic") == newest
    assert store.topic_outlines.latest("missing") is None
    assert store.topic_outlines.get("missing") is None


def test_all_read_methods_only_select_frozen_results(store):
    original = store.topic_outlines.add(result())
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lstrip().upper())

    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        assert store.topic_outlines.get("outline") == original
        assert store.topic_outlines.by_fingerprint("topic", "hash", "input", "producer") == original
        assert store.topic_outlines.latest("topic") == original
        assert store.topic_outlines.history("topic") == (original,)
        assert store.topic_outlines.compatible("topic", "hash", "producer", "zh-CN", 12000) == (
            original,
        )
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    assert len(statements) == 5 and all(sql.startswith("SELECT") for sql in statements)


@pytest.mark.parametrize("limit", [0, -1, True, 1.0, "2"])
def test_history_requires_strict_positive_integer_limit(store, limit):
    with pytest.raises(ValueError, match="limit_invalid"):
        store.topic_outlines.history("topic", limit)


@pytest.mark.parametrize(
    "field,value",
    [
        ("topic_id", "other"),
        ("topic_hash", "old"),
        ("producer_version", "other"),
        ("reading_language", "en"),
        ("token_budget", 1000),
    ],
)
def test_compatible_history_isolates_all_configuration_dimensions(store, field, value):
    original = store.topic_outlines.add(result())
    store.topic_outlines.add(result("incompatible", input_hash="other", **{field: value}))
    assert store.topic_outlines.compatible("topic", "hash", "producer", "zh-CN", 12000) == (
        original,
    )
    assert store.topic_outlines.compatible("missing", "hash", "producer", "zh-CN", 12000) == ()


def test_compatible_returns_all_history_and_recovers_old_configuration(store):
    older = store.topic_outlines.add(result("older", created_at="2026-11-01T01:00:00+00:00"))
    saved = tuple(
        store.topic_outlines.add(result(f"outline-{n:02}", input_hash=f"input-{n}"))
        for n in range(25)
    )
    assert store.topic_outlines.compatible("topic", "hash", "producer", "zh-CN", 12000) == (
        *reversed(saved),
        older,
    )
    for n in range(25):
        store.topic_outlines.add(
            result(
                f"new-{n:02}",
                input_hash=f"en-{n}",
                reading_language="en",
                created_at="2026-11-12T00:00:00+00:00",
            )
        )
    assert older not in store.topic_outlines.history("topic")
    assert store.topic_outlines.latest("topic").reading_language == "en"
    assert len(store.topic_outlines.compatible("topic", "hash", "producer", "zh-CN", 12000)) == 26


@pytest.mark.parametrize("budget", [True, False, 12000.0, "12000", 0, 511, 64001])
def test_compatible_rejects_invalid_and_non_integer_budget(store, budget):
    with pytest.raises(ValidationError):
        store.topic_outlines.compatible("topic", "hash", "producer", "zh-CN", budget)


def test_compatible_rejects_unknown_language(store):
    with pytest.raises(ValueError, match="language_invalid"):
        store.topic_outlines.compatible("topic", "hash", "producer", "fr", 12000)


def test_missing_considered_paper_is_rejected_even_when_not_sent(store):
    with pytest.raises(ValueError, match="paper_missing"):
        store.topic_outlines.add(
            result(considered_paper_ids=("P", "missing"), ungrouped=("missing",))
        )
    assert store.topic_outlines.history("topic") == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"ungrouped": ("P",)},
        {"papers": ()},
        {"source_hashes": {}},
        {"gaps": (OutlineGap(description="unknown", paper_ids=("missing",)),)},
    ],
)
def test_model_copy_cannot_bypass_domain_partition_and_attachment_validation(store, changes):
    with pytest.raises(ValidationError):
        store.topic_outlines.add(result().model_copy(update=changes))
    assert store.topic_outlines.history("topic") == ()


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "topic_id",
        "topic_hash",
        "input_hash",
        "producer_version",
        "created_at",
        "model",
        "scope_id",
    ],
)
def test_blank_identifiers_are_rejected(store, field):
    with pytest.raises(ValueError, match="identifier_empty"):
        store.topic_outlines.add(result(**{field: " "}))


def test_concurrent_same_fingerprint_preserves_one_complete_original(tmp_path):
    url = f"sqlite:///{tmp_path / 'concurrent.db'}"
    upgrade_database(url)
    stores = [SqlStore(url), SqlStore(url)]
    stores[0].papers.insert(Paper("P", "Original", "original", None, (), None, None, None, "t"))
    barrier = Barrier(2)

    def create(index):
        barrier.wait()
        return stores[index].topic_outlines.add(result(f"outline-{index}", model=f"model-{index}"))

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            saved = list(executor.map(create, range(2)))
        assert saved[0] == saved[1]
        assert stores[0].topic_outlines.history("topic") == (saved[0],)
        assert saved[0].model == f"model-{saved[0].id[-1]}"
        assert saved[0].papers == result().papers
    finally:
        for ctx in stores:
            ctx.dispose()


def test_thousand_paper_validation_uses_two_selects_and_one_id_parameter(store):
    ids = tuple(f"P{n:04}" for n in range(1000))
    with store.atomic():
        for id_ in ids:
            store.papers.insert(Paper(id_, id_, id_, None, (), None, None, None, "t"))
    with store._db._engine.connect() as connection:
        connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    selects = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append((statement, parameters))

    event.listen(store._db._engine, "before_cursor_execute", capture)
    try:
        stored = store.topic_outlines.add(result(paper_ids=ids))
    finally:
        event.remove(store._db._engine, "before_cursor_execute", capture)
    assert stored.considered_paper_ids == ids
    assert len(selects) == 2
    assert "json_each" in selects[1][0] and len(selects[1][1]) == 1
    assert store.topic_outlines.get(stored.id) == stored
