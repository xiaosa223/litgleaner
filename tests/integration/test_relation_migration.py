"""0019仅登记既有文库基线；SQLite边界、旧反馈和迁移往返可核对。"""

import json

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, select, text
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.exc import IntegrityError
from tests.integration.relation_migration_helpers import (
    CAPTURE_TRIGGERS,
    LEARNING_TRIGGERS,
    RELATION_TRIGGERS,
    assert_legacy_preserved,
    assert_search_upgrade_triggers,
)
from tests.integration.test_memory_repo import add_inbox, add_note, add_paper
from tests.integration.test_relation_repo import batch, proposal
from tests.integration.test_topic_map_migration import _snapshot

from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.infra.db.schema import memory_admission, metadata, paper_relation, relate_batch
from litgleaner.infra.db.schema_rules import include_schema_object
from litgleaner.infra.db.store import SqlStore


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'relations.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    for key in ("A", "B", "C"):
        add_paper(store, key, "read")
    yield store
    store.dispose()


def legacy(path):
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0018")
    store = SqlStore(url)
    try:
        for paper_id in ("C", "B", "A", "deleted", "pending", "external"):
            add_paper(store, paper_id, "abandoned" if paper_id == "A" else None)
        add_note(store, "B")
        add_inbox(store, "C", "done")
        note = add_note(store, "deleted")
        store.reader.delete_annotation(note.id, "later")
        add_inbox(store, "pending", "pending")
        store._db.write(
            text(
                "INSERT INTO feedback(id,paper_id,channel,action,external_id,created_at) "
                "VALUES('old','A','reader','save_to_library','old-request','t')"
            )
        )
    finally:
        store.dispose()
    return url


def test_baseline_is_deterministic_scoped_without_jobs_calls_or_implicit_reentry(tmp_path):
    url = legacy(tmp_path / "legacy.db")
    upgrade_database(url)
    upgrade_database(url)
    store = SqlStore(url)
    try:
        records = tuple(store.admissions.get(key) for key in ("A", "B", "C"))
        assert tuple(item.paper_id for item in records) == ("A", "B", "C")
        assert tuple(item.id for item in records) == (1, 2, 3)
        assert all(item.baseline and item.configuration == {} for item in records)
        assert all(store.admissions.get(key) is None for key in ("deleted", "pending", "external"))
        assert store.admissions.add("A", {"route": "later"}, "new-time") == records[0]
        assert store._db.fetch(text("SELECT count(*) FROM job"))[0][0] == 0
        assert store._db.fetch(text("SELECT count(*) FROM llm_call"))[0][0] == 0
        assert store.relations.batches_for_paper("A") == ()
        assert store.feedback.by_external_id("old-request").payload is None
        assert store.feedback.pending_saves("library") == ("A",)
    finally:
        store.dispose()


def test_0019_preserves_all_old_data_constraints_and_fts_and_matches_complete_metadata(tmp_path):
    path = tmp_path / "legacy.db"
    url = legacy(path)
    before, keys, definitions, triggers = _snapshot(path)
    upgrade_database(url)
    after, current_keys, current_definitions, current_triggers = _snapshot(path, before)
    assert_legacy_preserved(
        before,
        after,
        keys,
        current_keys,
        definitions=definitions,
        current_definitions=current_definitions,
    )
    assert_search_upgrade_triggers(triggers, current_triggers)
    assert (
        set(current_triggers) - set(triggers)
        == RELATION_TRIGGERS | CAPTURE_TRIGGERS | LEARNING_TRIGGERS
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
    command.downgrade(alembic_config(url), "0018")
    assert _snapshot(path) == (before, keys, definitions, triggers)
    upgrade_database(url)
    store = SqlStore(url)
    try:
        assert store.admissions.get("A").baseline
        assert store.feedback.latest_action("A") == "save_to_library"
    finally:
        store.dispose()


def test_feedback_new_foreign_key_does_not_allow_an_unknown_relation(store):
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        store._db.write(
            text(
                "INSERT INTO feedback(id,paper_id,channel,action,created_at,relation_id) "
                "VALUES('bad','A','reader','confirm','t','missing')"
            )
        )
    assert store.feedback.by_external_id("bad") is None


@pytest.mark.parametrize("payload", ["bad-json", "[]", "false"])
def test_feedback_payload_accepts_only_optional_json_objects(store, payload):
    with pytest.raises(IntegrityError):
        store._db.write(
            text(
                "INSERT INTO feedback(id,paper_id,channel,action,created_at,payload) "
                "VALUES('bad','A','reader','confirm','t',:payload)"
            ).bindparams(payload=payload)
        )


@pytest.mark.parametrize(
    "changes", [{"id": "replacement"}, {"input_hash": "changed"}, {"rowid": -1}]
)
def test_batch_replacement_and_nonpositive_rowid_cannot_hide_cache_history(store, changes):
    store.relations.add_batch(batch())
    original = dict(store._db.fetch(select(relate_batch))[0]._mapping)
    values = original | changes
    statement = text(
        f"INSERT OR REPLACE INTO relate_batch ({','.join(values)}) "
        f"VALUES ({','.join(':' + name for name in values)})"
    ).bindparams(**values)
    with pytest.raises(IntegrityError):
        store._db.write(statement)
    assert store.relations.batch("batch") == batch()


@pytest.mark.parametrize("data", ["not-json", "[]", "{}"])
def test_batch_malformed_or_unmirrored_json_is_rejected(store, data):
    values = {
        name: getattr(batch(), name)
        for name in ("id", "paper_id", "input_hash", "producer_version", "created_at")
    }
    with pytest.raises(IntegrityError):
        store._db.write(insert(relate_batch).values(**values, data=data))


@pytest.mark.parametrize(
    "path,value",
    [
        ("paper_a", "B"),
        ("evidence_a.paper_id", "B"),
        ("evidence_b.page", -1),
        ("evidence_a.page", True),
        ("evidence_b.quote", "short"),
        ("evidence_b.doc_key", " "),
        ("input_hash", "other-input"),
        ("producer_version", "other-producer"),
    ],
)
def test_raw_sql_cannot_corrupt_frozen_identity_provenance_or_evidence(store, path, value):
    store.relations.add_batch(batch())
    row = dict(store._db.fetch(select(paper_relation))[0]._mapping)
    original = json.loads(row["data"])
    changed = original | {
        "id": "raw-attempt",
        "batch_id": "other-batch",
        "input_hash": "other-batch",
    }
    target = changed
    parts = path.split(".")
    for key in parts[:-1]:
        target = target[key]
    target[parts[-1]] = value
    row.update(id="raw-attempt", batch_id="other-batch", data=json.dumps(changed))
    # 新批次使唯一性保护不先于内容护栏命中。
    store.relations.add_batch(batch("other-batch", relations=()))
    changed["batch_id"] = "other-batch"
    row["data"] = json.dumps(changed)
    with pytest.raises(IntegrityError):
        store._db.write(insert(paper_relation).values(**row))


def test_sql_self_link_missing_endpoint_and_cross_pair_supersession_are_rejected(store):
    store.relations.add_batch(batch())
    old = store.relations.for_paper("A")[0]
    store.relations.add_batch(
        batch(
            "ac",
            relations=(proposal("A", "C"),),
            source_hashes={"A": "source-A", "C": "source-C"},
            considered_paper_ids=("A", "C"),
            input_paper_ids=("A", "C"),
        )
    )
    other = store.relations.for_paper("C")[0]
    with pytest.raises(IntegrityError, match="paper_relation_supersession_invalid"):
        store._db.write(
            text(
                "UPDATE paper_relation SET status='superseded',superseded_by=:other WHERE id=:old"
            ).bindparams(other=other.id, old=old.id)
        )
    row = dict(
        store._db.fetch(select(paper_relation).where(paper_relation.c.id == old.id))[0]._mapping
    )
    for value in ("A", "missing"):
        with pytest.raises(IntegrityError):
            store._db.write(
                insert(paper_relation).values(**(row | {"id": "invalid", "paper_b": value}))
            )


def test_admission_replace_cannot_steal_an_earlier_order_or_change_configuration(store):
    original = store.admissions.add("A", {"frozen": True}, "t")
    with pytest.raises(IntegrityError, match="memory_admission_immutable"):
        store._db.write(
            insert(memory_admission)
            .prefix_with("OR REPLACE")
            .values(
                id=original.id, paper_id="B", configuration="{}", baseline=False, created_at="later"
            )
        )
    assert store.admissions.get("A") == original and store.admissions.get("B") is None
