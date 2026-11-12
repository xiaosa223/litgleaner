"""规范化重建检查逻辑记录，内部FTS页数缩减不冒充业务数据丢失。"""

import hashlib
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from tests.integration.search_normalization_helpers import legacy_database, raw_rows

from litgleaner.infra.db import backup
from litgleaner.infra.db.backup import check_snapshot, create_snapshot
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema_rules import FTS_TABLES

NOW = datetime(2026, 11, 12, tzinfo=UTC)


def test_old_snapshot_upgrade_keeps_logical_rows_when_internal_config_rows_shrink(tmp_path):
    path = tmp_path / "old.db"
    _, db = legacy_database(path)
    try:
        db.write(text("INSERT INTO memory_fts(memory_fts,rank) VALUES ('automerge',0)"))
        db.write(text("INSERT INTO memory_fts(memory_fts,rank) VALUES ('crisismerge',0)"))
        expected = raw_rows(db)
    finally:
        db.dispose()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = check_snapshot(path)
    assert result.ok, result.problems
    assert result.needs_upgrade and result.revision == "0019"
    assert result.row_counts["memory_fts"] == len(expected)
    assert result.row_counts["memory_fts_config"] == 3
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_new_snapshot_remains_readable_without_udf_registration(tmp_path):
    path = tmp_path / "current.db"
    url, db = legacy_database(path)
    try:
        upgrade_database(url)
        expected = raw_rows(db)
        snapshot = create_snapshot(path, tmp_path / "backups", keep=2, now=NOW).path
        with closing(sqlite3.connect(snapshot)) as connection:
            rows = connection.execute("SELECT count(*) FROM memory_fts").fetchone()[0]
            assert rows == len(expected)
            connection.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
        result = check_snapshot(snapshot)
        assert result.ok and not result.needs_upgrade
    finally:
        db.dispose()


@pytest.mark.parametrize("table", ["source_cursor", "memory_fts", "memory_fts_future"])
def test_real_business_logical_and_unknown_table_shrink_remains_an_error(
    tmp_path, monkeypatch, table
):
    path = tmp_path / "old.db"
    url, db = legacy_database(path)
    db.write(text("INSERT INTO source_cursor VALUES ('source','query',NULL,'t')"))
    if table == "memory_fts_future":
        db.write(text("CREATE TABLE memory_fts_future(id INTEGER)"))
        db.write(text("INSERT INTO memory_fts_future VALUES (1)"))
    before = backup._inspect(path)[1]
    db.dispose()
    upgrade_database(url)
    current = Database(url)
    current.write(text(f'DELETE FROM "{table}"'))
    current.dispose()
    monkeypatch.setattr(backup, "upgrade_database", lambda _: None)
    problems = backup._upgrade_problems(path, before)
    assert any("升级后行数减少" in item and table in item for item in problems)


def test_only_known_fts_shadow_counts_are_excluded_from_upgrade_loss_check(tmp_path):
    path = tmp_path / "old.db"
    _, db = legacy_database(path)
    before = backup._inspect(path)[1]
    db.dispose()
    for name in FTS_TABLES - {"memory_fts"}:
        before[name] += 10000
    assert backup._upgrade_problems(path, before) == []


def test_fts_integrity_failure_is_explicitly_reported(tmp_path):
    path = tmp_path / "current.db"
    url, db = legacy_database(path)
    db.dispose()
    upgrade_database(url)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("DELETE FROM memory_fts_docsize")
        connection.commit()
    result = check_snapshot(path)
    assert not result.ok
    assert any("全文索引完整性检查未通过" in item for item in result.problems)
