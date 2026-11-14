"""0017 仅新增综述结果，升级降级保留全部旧事实与FTS。"""

import sqlite3

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from tests.integration.relation_migration_helpers import (
    CAPTURE_TRIGGERS,
    LEARNING_TRIGGERS,
    RELATION_TRIGGERS,
    assert_legacy_preserved,
    assert_search_upgrade_triggers,
)
from tests.integration.test_outline_repo import result
from tests.integration.test_topic_map_migration import _legacy_survey, _snapshot
from tests.integration.test_topic_map_repo import result as map_result

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.infra.db.schema import metadata
from litgleaner.infra.db.schema_rules import include_schema_object
from litgleaner.infra.db.store import SqlStore


def _legacy_map(path):
    url = _legacy_survey(path)
    command.upgrade(alembic_config(url), "0016")
    store = SqlStore(url)
    try:
        store.topic_maps.add(map_result())
    finally:
        store.dispose()
    return url


def test_0017_preserves_old_tables_facts_keys_triggers_and_matches_metadata(tmp_path):
    path = tmp_path / "legacy.db"
    url = _legacy_map(path)
    before, keys, definitions, triggers = _snapshot(path)
    upgrade_database(url)
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
        == {
            "topic_outline_json",
            "topic_outline_no_replace",
            "topic_outline_no_update",
            "topic_outline_no_delete",
            "paper_embedding_no_replace",
            "paper_embedding_no_update",
            "paper_embedding_no_delete",
            "paper_embedding_source",
            "paper_embedding_rowid",
        }
        | RELATION_TRIGGERS
        | CAPTURE_TRIGGERS
        | LEARNING_TRIGGERS
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
    try:
        assert store.topic_maps.get("map") == map_result()
        assert store.surveys.get_work("run", 1).paper_ids == ("P",)
        assert store.runs.llm_cost_for_scope("old-scope") == 0.2
    finally:
        store.dispose()


def test_0017_downgrade_drops_only_outline_and_restores_old_schema_exactly(tmp_path):
    path = tmp_path / "roundtrip.db"
    url = _legacy_map(path)
    before = _snapshot(path)
    upgrade_database(url)
    store = SqlStore(url)
    try:
        store.topic_outlines.add(result())
    finally:
        store.dispose()
    command.downgrade(alembic_config(url), "0016")
    assert _snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'topic_outline%'"
            ).fetchall()
            == []
        )
        connection.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
    upgrade_database(url)
    store = SqlStore(url)
    try:
        assert store.topic_outlines.history("topic") == ()
        assert store.topic_maps.get("map") == map_result()
    finally:
        store.dispose()


@pytest.mark.parametrize("data", ["broken", "null", "[]", "{}", '{"id":"wrong"}'])
def test_outline_table_rejects_malformed_json_and_missing_identity(tmp_path, data):
    _reject_data(tmp_path, data)


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "topic_id",
        "topic_hash",
        "input_hash",
        "producer_version",
        "created_at",
    ],
)
def test_every_json_identity_field_must_equal_its_persistent_column(tmp_path, field):
    _reject_data(tmp_path, result(**{field: "different"}).model_dump_json())


def _reject_data(tmp_path, data):
    url = f"sqlite:///{tmp_path / 'invalid.db'}"
    upgrade_database(url)
    database = Database(url)
    try:
        with pytest.raises(IntegrityError):
            database.write(
                text(
                    "INSERT INTO topic_outline(id,topic_id,topic_hash,input_hash,producer_version,"
                    "created_at,data) VALUES ('outline','topic','hash','input','producer',"
                    "'2026-11-11T01:00:00+00:00',:data)"
                ).bindparams(data=data)
            )
    finally:
        database.dispose()
