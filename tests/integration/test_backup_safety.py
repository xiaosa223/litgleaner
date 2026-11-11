"""可配置目录不能让保留清理误删活动数据库，备份锁失败必须可恢复。"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.integration.test_daily_backup import daily, make_database

from litgleaner.domain.errors import LitGleanerError
from litgleaner.infra.db import backup
from litgleaner.infra.db.backup import check_snapshot


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return make_database(tmp_path)


@pytest.mark.parametrize(
    "case",
    [
        "named",
        "daily",
        "existing",
        "alias",
        "casealias",
        "casealias_daily",
        "hardlink",
        "hardlink_daily",
    ],
)
def test_active_database_inside_backup_directory_is_never_pruned(
    database, tmp_path, monkeypatch, case
):
    directory = tmp_path / "source-backups"
    directory.mkdir()
    filename = (
        "litgleaner-20261107T010203Z-daily.db"
        if case in ("daily", "casealias_daily", "hardlink_daily")
        else "litgleaner-20260101T000000Z.db"
    )
    source = directory / filename
    shutil.copyfile(database, source)
    expected = daily(source, directory, keep=2) if case == "existing" else None
    if case == "alias":
        if backup.os.name == "nt":
            pytest.skip("相对目录符号链接身份检查在 POSIX 执行")
        (tmp_path / "alias").symlink_to(directory, target_is_directory=True)
        monkeypatch.chdir(tmp_path)
        source = Path("alias") / filename
    elif case.startswith("casealias"):
        source = source.with_name(source.name.upper())
        if not source.is_file():
            pytest.skip("大小写别名检查只在不区分大小写的文件系统执行")
    elif case.startswith("hardlink"):
        alias = tmp_path / "active.db"
        alias.hardlink_to(source)
        source = alias
    snapshot = daily(source, directory, keep=1)
    assert source.is_file() and check_snapshot(source).ok
    assert snapshot.is_file() and not snapshot.samefile(source)
    assert check_snapshot(snapshot).ok
    if expected is not None:
        assert snapshot == expected
    assert len(list(directory.glob("*.db"))) == 2  # 活动库不占保留份数。


def test_directory_lock_is_released_after_operation_failure(database, tmp_path, monkeypatch):
    directory = tmp_path / "b"
    original = backup._vacuum_into

    def fail(*_args):
        raise OSError("copy failed")

    with monkeypatch.context() as guard:
        guard.setattr(backup, "_vacuum_into", fail)
        with pytest.raises(LitGleanerError, match="copy failed"):
            daily(database, directory)
    assert (directory / ".backup.lock").is_file()
    assert backup._vacuum_into is original
    assert check_snapshot(daily(database, directory)).ok


def test_directory_creation_failure_is_reported_and_retryable(database, tmp_path):
    directory = tmp_path / "b"
    directory.write_text("blocked", encoding="utf-8")
    with pytest.raises(LitGleanerError, match="备份失败"):
        daily(database, directory)
    directory.unlink()
    assert check_snapshot(daily(database, directory)).ok


@pytest.mark.parametrize("stage", ["acquire", "release"])
def test_lock_errors_close_handle_and_keep_same_lock_file(database, tmp_path, monkeypatch, stage):
    directory, handles = tmp_path / "b", []
    original = backup._lock_file

    def fail(handle, *, acquire):
        handles.append(handle)
        if acquire == (stage == "acquire"):
            raise OSError("lock unavailable")
        return original(handle, acquire=acquire)

    with monkeypatch.context() as guard:
        guard.setattr(backup, "_lock_file", fail)
        with pytest.raises(LitGleanerError, match="lock unavailable"):
            daily(database, directory)
    assert all(handle.closed for handle in handles)
    inode = (directory / ".backup.lock").stat().st_ino
    assert check_snapshot(daily(database, directory)).ok
    assert (directory / ".backup.lock").stat().st_ino == inode


def test_windows_lock_contract_seeks_to_first_byte_for_acquire_and_release(tmp_path, monkeypatch):
    calls = []
    windows = SimpleNamespace(
        LK_LOCK=1, LK_UNLCK=2, locking=lambda fd, mode, size: calls.append((fd, mode, size))
    )
    monkeypatch.setattr(backup, "msvcrt", windows, raising=False)
    monkeypatch.setattr(backup, "sys", SimpleNamespace(platform="win32"))
    with (tmp_path / "lock").open("w+b") as handle:
        handle.write(b"0")
        backup._lock_file(handle, acquire=True)
        assert handle.tell() == 0
        handle.seek(1)
        backup._lock_file(handle, acquire=False)
        assert handle.tell() == 0
        assert calls == [(handle.fileno(), 1, 1), (handle.fileno(), 2, 1)]
