"""0016 只追加地图表，旧事实、调研恢复数据和全文索引保持原样。"""

import sqlite3

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from tests.integration.relation_migration_helpers import (
    CAPTURE_TRIGGERS,
    FTS_RAW_COLUMNS,
    RELATION_TRIGGERS,
    assert_legacy_preserved,
    assert_search_upgrade_triggers,
)
from tests.integration.test_survey_migration import _legacy
from tests.integration.test_survey_repo import preview, round_item, work
from tests.integration.test_topic_map_repo import result

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.infra.db.schema import metadata
from litgleaner.infra.db.schema_rules import FTS_TABLES, include_schema_object
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import JobSpec, LLMCall


def _legacy_survey(path):
    url = _legacy(path)
    command.upgrade(alembic_config(url), "0015")
    store = SqlStore(url)
    try:
        store.surveys.add_preview(preview())
        store.surveys.accept("plan", "run", "t")
        store.surveys.save_work(work())
        store.surveys.append_round(round_item())
        store.runs.record_llm_call(
            LLMCall("ask", "old", "v", 1, 2, 0.2, True, "P", "old-scope"), "t"
        )
        store.jobs.enqueue(JobSpec("pending", "old", {"paper_id": "P"}), "t")
    finally:
        store.dispose()
    return url


def _snapshot(path, names=None):
    with sqlite3.connect(path) as connection:
        if names is None:
            names = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if row[0] not in FTS_TABLES - {"memory_fts"} and row[0] != "alembic_version"
            }
        rows = {
            name: connection.execute(
                f"SELECT {FTS_RAW_COLUMNS if name == 'memory_fts' else '*'} "
                f'FROM "{name}" ORDER BY rowid'
            ).fetchall()
            for name in names
        }
        keys = {
            name: connection.execute(f'PRAGMA foreign_key_list("{name}")').fetchall()
            for name in names
        }
        definitions = {
            name: connection.execute(
                "SELECT sql,rootpage FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            for name in names
        }
        triggers = dict(
            connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'")
        )
    return rows, keys, definitions, triggers


def test_0016_preserves_existing_tables_facts_keys_triggers_and_matches_metadata(tmp_path):
    path = tmp_path / "legacy.db"
    url = _legacy_survey(path)
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
            "topic_map_json",
            "topic_map_no_update",
            "topic_map_no_delete",
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
        assert store.surveys.get_work("run", 1).paper_ids == ("P",)
        assert store.surveys.round_for("run", 1).cost_usd == 0.1
        assert store.runs.llm_cost_for_scope("old-scope") == 0.2
        assert store.topic_maps.latest("topic") is None
    finally:
        store.dispose()


def test_0016_downgrade_drops_only_map_and_restores_old_schema_exactly(tmp_path):
    path = tmp_path / "roundtrip.db"
    url = _legacy_survey(path)
    before = _snapshot(path)
    upgrade_database(url)
    store = SqlStore(url)
    try:
        store.topic_maps.add(result())
    finally:
        store.dispose()
    command.downgrade(alembic_config(url), "0015")
    assert _snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'topic_map%'"
            ).fetchall()
            == []
        )
        connection.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
    upgrade_database(url)
    store = SqlStore(url)
    try:
        assert store.topic_maps.history("topic") == ()
        assert store.surveys.round_for("run", 1).cost_usd == 0.1
    finally:
        store.dispose()


@pytest.mark.parametrize("data", ["broken", "null", "[]", "{}", '{"id":"wrong"}'])
def test_map_table_rejects_malformed_json_or_wrong_identity(tmp_path, data):
    url = f"sqlite:///{tmp_path / 'invalid.db'}"
    upgrade_database(url)
    database = Database(url)
    try:
        with pytest.raises(IntegrityError):
            database.write(
                text(
                    "INSERT INTO topic_map(id,topic_id,topic_hash,input_hash,"
                    "producer_version,created_at,data) "
                    "VALUES ('map','topic','hash','input','producer','t',:data)"
                ).bindparams(data=data)
            )
    finally:
        database.dispose()
