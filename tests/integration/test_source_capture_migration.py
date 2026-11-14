"""采集表迁移可逆，DDL 中途失败不留下半套表或错误版本。"""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from tests.integration.test_source_capture_repo import attempt, item

from litgleaner.domain.paper import Paper
from litgleaner.infra.db.migrate import alembic_config, current_revision, upgrade_database
from litgleaner.infra.db.schema import metadata
from litgleaner.infra.db.schema_rules import include_schema_object
from litgleaner.infra.db.store import SqlStore


@pytest.fixture
def legacy(tmp_path):
    url = f"sqlite:///{tmp_path / 'capture-upgrade.db'}"
    command.upgrade(alembic_config(url), "0020")
    store = SqlStore(url)
    original = Paper("P", "Original", "original", "Old abstract", (), None, None, None, "t")
    store.papers.insert(original)
    yield url, store, original
    store.dispose()


def _schema(store):
    return store._db.fetch(
        text(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )


def test_upgrade_downgrade_and_reupgrade_match_metadata_without_changing_old_tables(legacy):
    url, store, original = legacy
    before = set(inspect(store._db._engine).get_table_names())
    upgrade_database(url)
    assert store.papers.get("P") == original
    with store._db._engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"include_object": include_schema_object}
        )
        assert compare_metadata(context, metadata) == []
    store.source_captures.start(attempt())
    store.source_captures.append(item())
    command.downgrade(alembic_config(url), "0020")
    assert set(inspect(store._db._engine).get_table_names()) == before
    assert store.papers.get("P") == original
    upgrade_database(url)
    assert store.source_captures.list("alpha") == ()
    assert store.papers.get("P") == original
    store.source_captures.start(attempt())


@pytest.mark.parametrize(
    "failure",
    [
        "CREATE TABLE source_capture_input",
        "CREATE TRIGGER source_capture_input_no_update",
        "UPDATE alembic_version",
    ],
)
def test_upgrade_ddl_failure_rolls_back_new_tables_version_and_keeps_old_records(legacy, failure):
    url, store, original = legacy
    schema = _schema(store)

    def fail(_conn, _cursor, statement, _params, _context, _many):
        if statement.strip().startswith(failure):
            raise RuntimeError("injected capture DDL failure")

    event.listen(Engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            upgrade_database(url)
    finally:
        event.remove(Engine, "before_cursor_execute", fail)
    assert current_revision(url) == "0020"
    assert _schema(store) == schema
    assert store.papers.get("P") == original
    upgrade_database(url)
    store.source_captures.start(attempt())


@pytest.mark.parametrize("failure", ["DROP TABLE source_capture_attempt", "UPDATE alembic_version"])
def test_downgrade_failure_restores_both_tables_inputs_and_version_before_retry(legacy, failure):
    url, store, original = legacy
    command.upgrade(alembic_config(url), "0021")  # 只测 0021 自身的降级，不受之后迁移影响
    store.source_captures.start(attempt())
    store.source_captures.append(item())
    schema = _schema(store)

    def fail(_conn, _cursor, statement, _params, _context, _many):
        if statement.strip().startswith(failure):
            raise RuntimeError("injected capture downgrade failure")

    event.listen(Engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            command.downgrade(alembic_config(url), "0020")
    finally:
        event.remove(Engine, "before_cursor_execute", fail)
    assert current_revision(url) == "0021"
    assert _schema(store) == schema
    assert store.source_captures.get("01CAPTURE") == attempt()
    assert store.source_captures.inputs("01CAPTURE") == (item(),)
    assert store.papers.get("P") == original
    command.downgrade(alembic_config(url), "0020")
    upgrade_database(url)
    store.source_captures.start(attempt())
