"""每日快照按配置时区识别成功文件，重启、失败与保留清理均可恢复。"""

import hashlib
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from litgleaner.domain.errors import LitGleanerError
from litgleaner.infra.db import backup
from litgleaner.infra.db.backup import check_snapshot, create_snapshot, ensure_daily_snapshot
from litgleaner.infra.db.migrate import upgrade_database

NOW = datetime(2026, 11, 7, 1, 2, 3, tzinfo=UTC)


def make_database(tmp_path: Path) -> Path:
    path = tmp_path / "数据 目录" / "litgleaner.db"
    upgrade_database(f"sqlite:///{path}")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO source_cursor (source, query_hash, cursor, updated_at) "
            "VALUES ('alpha', 'q', NULL, '2026-11-07')"
        )
    connection.close()
    return path


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return make_database(tmp_path)


def daily(database, directory, now=NOW, *, keep=14, timezone=UTC):
    return ensure_daily_snapshot(database, directory, keep=keep, now=now, timezone=timezone)


def forbidden(*_args, **_kwargs):
    pytest.fail("已有当天成功快照，不得再次 VACUUM")


def test_first_snapshot_is_complete_and_supports_restore_check(database, tmp_path):
    path = daily(database, tmp_path / "自定义 备份")
    report = check_snapshot(path)
    assert path.name == "litgleaner-20261107T010203Z-daily.db"
    assert report.ok and report.row_counts["source_cursor"] == 1
    assert [p.name for p in path.parent.glob(".*")] == [".backup.lock"]


def test_same_day_and_fresh_call_reuse_completed_snapshot(database, tmp_path, monkeypatch):
    directory = tmp_path / "backups"
    first = daily(database, directory)
    monkeypatch.setattr(backup, "_vacuum_into", forbidden)
    assert daily(database, directory, NOW + timedelta(hours=12)) == first
    assert daily(database, directory, NOW + timedelta(hours=5)) == first
    assert len(list(directory.glob("*.db"))) == 1


def test_local_day_can_span_two_utc_dates(database, tmp_path, monkeypatch):
    zone = ZoneInfo("Asia/Shanghai")
    first_at = datetime(2026, 11, 6, 23, 30, tzinfo=UTC)
    first = daily(database, tmp_path / "b", first_at, timezone=zone)
    monkeypatch.setattr(backup, "_vacuum_into", forbidden)
    assert daily(database, tmp_path / "b", first_at + timedelta(hours=2), timezone=zone) == first


def test_local_midnight_creates_snapshot_within_same_utc_date(database, tmp_path):
    zone = ZoneInfo("Asia/Shanghai")
    before = datetime(2026, 11, 7, 15, 59, tzinfo=UTC)
    first = daily(database, tmp_path / "b", before, timezone=zone)
    second = daily(database, tmp_path / "b", before + timedelta(minutes=2), timezone=zone)
    assert first != second and first.is_file() and second.is_file()


def test_dst_repeated_hour_stays_in_same_daily_snapshot(database, tmp_path, monkeypatch):
    zone = ZoneInfo("America/New_York")
    before = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    first = daily(database, tmp_path / "b", before, timezone=zone)
    monkeypatch.setattr(backup, "_vacuum_into", forbidden)
    assert daily(database, tmp_path / "b", before + timedelta(hours=1), timezone=zone) == first


def test_manual_and_upgrade_snapshots_do_not_suppress_daily_backup(database, tmp_path):
    directory = tmp_path / "b"
    manual = create_snapshot(database, directory, keep=14, now=NOW).path
    upgrade = create_snapshot(database, directory, keep=14, now=NOW, label="pre-upgrade-0016").path
    automatic = daily(database, directory)
    assert automatic not in (manual, upgrade)
    assert len(list(directory.glob("*.db"))) == 3


@pytest.mark.parametrize(
    "name",
    [
        ".litgleaner-20261107T010203Z-daily.db.partial",
        "litgleaner-20261107T010203Z-daily.db.partial",
        "litgleaner-20261307T010203Z-daily.db",
        "litgleaner-20261107T010203Z-daily-other.db",
        "litgleaner-20261107T010203Z-daily-0.db",
        "litgleaner-20261107T010203Z-daily-1.db",
        "litgleaner-20261108T010203Z-daily.db",
    ],
)
def test_partial_invalid_names_and_other_days_do_not_suppress_backup(database, tmp_path, name):
    directory = tmp_path / "b"
    directory.mkdir()
    (directory / name).write_text("incomplete or unrelated", encoding="utf-8")
    assert daily(database, directory).name == "litgleaner-20261107T010203Z-daily.db"


@pytest.mark.parametrize("kind", ["empty", "directory"])
def test_empty_file_or_directory_is_not_a_completed_snapshot(database, tmp_path, kind):
    directory = tmp_path / "b"
    directory.mkdir()
    invalid = directory / "litgleaner-20261107T010203Z-daily.db"
    invalid.touch() if kind == "empty" else invalid.mkdir()
    result = daily(database, directory)
    assert result != invalid and result.is_file() and check_snapshot(result).ok


def test_default_retention_keeps_fourteen_successful_daily_snapshots(database, tmp_path):
    directory = tmp_path / "b"
    paths = [daily(database, directory, NOW + timedelta(days=day)) for day in range(16)]
    assert len(list(directory.glob("*.db"))) == 14
    assert not paths[0].exists() and not paths[1].exists()
    assert all(path.is_file() for path in paths[2:])


def test_same_day_call_honors_changed_keep_and_preserves_user_files(
    database, tmp_path, monkeypatch
):
    directory = tmp_path / "b"
    previous = [daily(database, directory, NOW - timedelta(days=day)) for day in (2, 1)]
    current = daily(database, directory)
    notes = directory / "notes.txt"
    notes.write_text("保留用户文件", encoding="utf-8")
    monkeypatch.setattr(backup, "_vacuum_into", forbidden)
    assert daily(database, directory, keep=1) == current
    assert not any(path.exists() for path in previous)
    assert notes.read_text(encoding="utf-8") == "保留用户文件"


def test_clock_rollback_keeps_the_new_successful_daily_snapshot(database, tmp_path):
    directory = tmp_path / "b"
    future = daily(database, directory, NOW + timedelta(days=2), keep=1)
    current = daily(database, directory, NOW, keep=1)
    assert current.is_file() and not future.exists()
    assert check_snapshot(current).ok


def test_reusing_today_preserves_it_while_pruning_future_snapshot(database, tmp_path, monkeypatch):
    directory = tmp_path / "b"
    today = daily(database, directory)
    future = daily(database, directory, NOW + timedelta(days=2))
    monkeypatch.setattr(backup, "_vacuum_into", forbidden)
    assert daily(database, directory, keep=1) == today
    assert today.is_file() and not future.exists()


def test_failed_vacuum_keeps_previous_and_retry_creates_today(database, tmp_path, monkeypatch):
    directory = tmp_path / "b"
    previous = daily(database, directory, NOW - timedelta(days=1))
    digest = hashlib.sha256(previous.read_bytes()).hexdigest()
    with monkeypatch.context() as guard:
        guard.setattr(
            backup,
            "_vacuum_into",
            lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
        )
        with pytest.raises(LitGleanerError, match="备份失败"):
            daily(database, directory, keep=1)
    assert hashlib.sha256(previous.read_bytes()).hexdigest() == digest
    current = daily(database, directory, keep=1)
    assert current.is_file() and not previous.exists()


def test_failed_atomic_replace_cleans_partial_and_preserves_previous(
    database, tmp_path, monkeypatch
):
    directory = tmp_path / "b"
    previous = daily(database, directory, NOW - timedelta(days=1))

    def fail(*_args):
        raise OSError("replace failed")

    with monkeypatch.context() as guard:
        guard.setattr(backup.os, "replace", fail)
        with pytest.raises(LitGleanerError, match="replace failed"):
            daily(database, directory, keep=1)
    assert previous.is_file() and not list(directory.glob(".*.partial"))
    assert daily(database, directory, keep=1).is_file()


def test_prune_failure_retries_existing_completed_snapshot_without_vacuum(
    database, tmp_path, monkeypatch
):
    directory = tmp_path / "b"
    previous = daily(database, directory, NOW - timedelta(days=1))
    unlink = Path.unlink

    def blocked(path, *args, **kwargs):
        if path == previous:
            raise OSError("prune blocked")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(Path, "unlink", blocked)
        with pytest.raises(LitGleanerError, match="prune blocked"):
            daily(database, directory, keep=1)
    current = directory / "litgleaner-20261107T010203Z-daily.db"
    assert current.is_file() and previous.is_file()
    monkeypatch.setattr(backup, "_vacuum_into", forbidden)
    assert daily(database, directory, keep=1) == current
    assert current.is_file() and not previous.exists()


def test_invalid_keep_is_rejected_even_when_today_exists(database, tmp_path):
    current = daily(database, tmp_path / "b")
    with pytest.raises(ValueError, match="keep"):
        daily(database, tmp_path / "b", keep=0)
    assert current.is_file()


def test_failed_directory_inspection_preserves_history_and_can_retry(
    database, tmp_path, monkeypatch
):
    directory = tmp_path / "b"
    previous = daily(database, directory, NOW - timedelta(days=1))
    iterdir = Path.iterdir

    def blocked(path):
        if path == directory:
            raise PermissionError("cannot inspect backups")
        return iterdir(path)

    with monkeypatch.context() as guard:
        guard.setattr(Path, "iterdir", blocked)
        with pytest.raises(LitGleanerError, match="cannot inspect backups"):
            daily(database, directory, keep=1)
    assert previous.is_file()
    current = daily(database, directory, keep=1)
    assert current.is_file() and not previous.exists()


@pytest.mark.parametrize(
    "name",
    [
        "litgleaner-20261307T010203Z.db",
        "litgleaner-20261132T010203Z-daily.db",
        "litgleaner-20261107T250203Z-pre-upgrade-0001.db",
        "litgleaner-not-a-date-daily.db",
        "notes.txt",
    ],
)
def test_retention_preserves_invalid_timestamp_names_and_unrelated_files(database, tmp_path, name):
    directory = tmp_path / "b"
    directory.mkdir()
    unrelated = directory / name
    unrelated.write_text("用户文件", encoding="utf-8")
    snapshot = daily(database, directory, keep=1)
    assert snapshot.is_file()
    assert unrelated.read_text(encoding="utf-8") == "用户文件"
