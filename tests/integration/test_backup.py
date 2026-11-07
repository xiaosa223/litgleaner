"""数据库快照、保留份数、恢复演练和升级前备份。"""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command

from litgleaner.domain.errors import LitGleanerError
from litgleaner.infra.db.backup import (
    check_snapshot,
    create_snapshot,
    upgrade_with_backup,
)
from litgleaner.infra.db.migrate import alembic_config, head_revision, upgrade_database

NOW = datetime(2026, 11, 7, 1, 2, 3, tzinfo=UTC)


def _url(path: Path) -> str:
    return f"sqlite:///{path}"


def _add_cursor(database: Path, source: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO source_cursor (source, query_hash, cursor, updated_at) "
            "VALUES (?, 'q', NULL, '2026-11-07')",
            (source,),
        )
    connection.close()


def _count(database: Path, table: str) -> int:
    connection = sqlite3.connect(database)
    try:
        return int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _revision(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        return str(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0])
    finally:
        connection.close()


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "data" / "中文 目录" / "litgleaner.db"
    upgrade_database(_url(path))
    _add_cursor(path, "alpha")
    return path


def _old_database(tmp_path: Path, revision: str = "0002") -> Path:
    path = tmp_path / "old" / "litgleaner.db"
    path.parent.mkdir(parents=True)
    command.upgrade(alembic_config(_url(path)), revision)
    _add_cursor(path, "alpha")
    return path


def test_snapshot_is_a_complete_copy(database: Path, tmp_path: Path):
    result = create_snapshot(database, tmp_path / "backups", keep=14, now=NOW)

    assert result.path.name == "litgleaner-20261107T010203Z.db"
    assert _count(result.path, "source_cursor") == 1
    assert _revision(result.path) == head_revision()
    assert not list((tmp_path / "backups").glob(".*"))


def test_snapshot_keeps_newest_and_ignores_other_files(database: Path, tmp_path: Path):
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "notes.txt").write_text("用户自己的文件", encoding="utf-8")
    for day in range(4):
        result = create_snapshot(database, backups, keep=2, now=NOW + timedelta(days=day))

    names = sorted(path.name for path in backups.iterdir())
    assert names == [
        "litgleaner-20261109T010203Z.db",
        "litgleaner-20261110T010203Z.db",
        "notes.txt",
    ]
    assert [path.name for path in result.removed] == ["litgleaner-20261108T010203Z.db"]


def test_snapshots_in_the_same_second_do_not_collide(database: Path, tmp_path: Path):
    first = create_snapshot(database, tmp_path / "b", keep=14, now=NOW)
    second = create_snapshot(database, tmp_path / "b", keep=14, now=NOW)

    assert first.path != second.path
    assert first.path.exists() and second.path.exists()


def test_pruning_within_one_second_keeps_the_latest(database: Path, tmp_path: Path):
    create_snapshot(database, tmp_path / "b", keep=1, now=NOW)
    latest = create_snapshot(database, tmp_path / "b", keep=1, now=NOW)

    assert [path.name for path in (tmp_path / "b").iterdir()] == [latest.path.name]


def test_snapshot_of_missing_database_fails_clearly(tmp_path: Path):
    with pytest.raises(LitGleanerError, match="数据库不存在"):
        create_snapshot(tmp_path / "none.db", tmp_path / "b", keep=14, now=NOW)
    assert not (tmp_path / "none.db").exists()


def test_check_passes_for_current_snapshot_and_leaves_it_untouched(database: Path, tmp_path: Path):
    snapshot = create_snapshot(database, tmp_path / "b", keep=14, now=NOW).path
    digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()

    report = check_snapshot(snapshot)

    assert report.ok, report.problems
    assert report.revision == head_revision()
    assert not report.needs_upgrade
    assert report.row_counts["source_cursor"] == 1
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == digest


def test_check_upgrades_old_snapshot_in_temporary_copy(tmp_path: Path):
    snapshot = _old_database(tmp_path)

    report = check_snapshot(snapshot)

    assert report.ok, report.problems
    assert report.revision == "0002"
    assert report.needs_upgrade
    assert report.row_counts["source_cursor"] == 1
    assert _revision(snapshot) == "0002"


def test_check_rejects_file_that_is_not_a_database(tmp_path: Path):
    snapshot = tmp_path / "broken.db"
    snapshot.write_bytes(b"not a database" * 100)

    report = check_snapshot(snapshot)

    assert not report.ok
    assert report.problems


def test_check_rejects_unknown_revision(database: Path, tmp_path: Path):
    snapshot = create_snapshot(database, tmp_path / "b", keep=14, now=NOW).path
    with sqlite3.connect(snapshot) as connection:
        connection.execute("UPDATE alembic_version SET version_num = 'from-the-future'")
    connection.close()

    report = check_snapshot(snapshot)

    assert not report.ok
    assert any("from-the-future" in problem for problem in report.problems)


def test_upgrade_backs_up_outdated_database_first(tmp_path: Path):
    database = _old_database(tmp_path)

    snapshot = upgrade_with_backup(database, tmp_path / "backups", keep=14, now=NOW)

    assert snapshot is not None
    assert snapshot.name == "litgleaner-20261107T010203Z-pre-upgrade-0002.db"
    assert _revision(snapshot) == "0002"
    assert _revision(database) == head_revision()
    assert _count(database, "source_cursor") == 1


def test_upgrade_skips_backup_for_new_or_current_database(database: Path, tmp_path: Path):
    backups = tmp_path / "backups"
    fresh = tmp_path / "fresh" / "litgleaner.db"

    assert upgrade_with_backup(fresh, backups, keep=14, now=NOW) is None
    assert upgrade_with_backup(database, backups, keep=14, now=NOW) is None
    assert _revision(fresh) == head_revision()
    assert not backups.exists()


def test_failed_backup_stops_the_migration(tmp_path: Path):
    database = _old_database(tmp_path)
    blocked = tmp_path / "backups"
    blocked.write_text("这里是一个文件，不是目录", encoding="utf-8")

    with pytest.raises(LitGleanerError, match="备份失败"):
        upgrade_with_backup(database, blocked, keep=14, now=NOW)

    assert _revision(database) == "0002"
