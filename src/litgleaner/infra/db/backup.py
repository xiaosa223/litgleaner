"""数据库快照、恢复演练和升级前备份。

快照用 SQLite 的 VACUUM INTO 生成：在一个读事务里得到一致的副本，流水线正在写入时也能备份。
恢复演练只在临时目录里操作，不碰原快照，也不碰正在使用的数据库。
"""

import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from typing import BinaryIO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from litgleaner.domain.errors import LitGleanerError
from litgleaner.infra.db.migrate import (
    current_revision,
    head_revision,
    known_revisions,
    upgrade_database,
)
from litgleaner.infra.db.schema_rules import FTS_TABLES

logger = logging.getLogger(__name__)

_PREFIX = "litgleaner-"
_SNAPSHOT = re.compile(r"^litgleaner-(\d{8}T\d{6}Z)(?:-[\w.-]+)?\.db$")
_DAILY = re.compile(r"^litgleaner-(\d{8}T\d{6}Z)-daily(?:-(?:[2-9]\d*|1\d+))?\.db$")
_VERSION_TABLE = "alembic_version"


@dataclass(frozen=True)
class BackupResult:
    path: Path
    removed: tuple[Path, ...]


@dataclass(frozen=True)
class SnapshotCheck:
    revision: str | None
    needs_upgrade: bool
    row_counts: Mapping[str, int] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


def _lock_file(handle: BinaryIO, *, acquire: bool) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if acquire else msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if acquire else fcntl.LOCK_UN)


@contextmanager
def _backup_lock(backup_dir: Path) -> Iterator[None]:
    """所有备份入口共用目录锁；固定保留锁文件，避免并发进程锁到不同 inode。"""
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
        with (backup_dir / ".backup.lock").open("a+b") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")  # Windows 的字节范围锁需要文件至少有一个字节。
                handle.flush()
            _lock_file(handle, acquire=True)
            try:
                yield
            finally:
                _lock_file(handle, acquire=False)
    except (OSError, sqlite3.Error) as error:
        raise LitGleanerError(f"备份失败：{error}") from error


def _target(backup_dir: Path, now: datetime, label: str) -> Path:
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{label}" if label else ""
    path = backup_dir / f"{_PREFIX}{stamp}{suffix}.db"
    counter = 2
    while path.exists():
        path = backup_dir / f"{_PREFIX}{stamp}{suffix}-{counter}.db"
        counter += 1
    return path


def _vacuum_into(database: Path, target: Path) -> None:
    partial = target.with_name(f".{target.name}.partial")
    partial.unlink(missing_ok=True)
    try:
        connection = sqlite3.connect(database)
        try:
            connection.execute("VACUUM INTO ?", (str(partial),))
        finally:
            connection.close()
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _prune(
    backup_dir: Path, keep: int, *, database: Path, preserve: Path | None = None
) -> tuple[Path, ...]:
    # 保留本次已成功的快照，避免系统时钟回拨后刚写完就被删；其他先按时间，再按写入先后。
    snapshots = sorted(
        (p for p in backup_dir.iterdir() if _stamp(p) and p.is_file() and not p.samefile(database)),
        key=lambda p: (p == preserve, _stamp(p), p.stat().st_mtime_ns),
    )
    removed = tuple(snapshots[:-keep]) if len(snapshots) > keep else ()
    for path in removed:
        path.unlink()
    return removed


def _stamp(path: Path) -> str:
    match = _SNAPSHOT.fullmatch(path.name)
    if match is None:
        return ""
    try:
        datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return ""
    return match.group(1)


def create_snapshot(
    database: Path, backup_dir: Path, *, keep: int, now: datetime, label: str = ""
) -> BackupResult:
    """生成快照，再按保留份数删除最旧的快照。只删除本程序命名的快照文件。"""
    if keep < 1:
        raise ValueError("keep 至少为 1")
    if not database.is_file():
        raise LitGleanerError(f"备份失败：数据库不存在：{database}")
    with _backup_lock(backup_dir):
        return _create_snapshot(database, backup_dir, keep=keep, now=now, label=label)


def _create_snapshot(
    database: Path, backup_dir: Path, *, keep: int, now: datetime, label: str
) -> BackupResult:
    """调用方已持有目录锁，覆盖选名、发布和保留清理的完整操作。"""
    target = _target(backup_dir, now, label)
    _vacuum_into(database, target)
    removed = _prune(backup_dir, keep, database=database, preserve=target)
    logger.info("数据库快照已写入 %s，清理旧快照 %d 份", target, len(removed))
    return BackupResult(target, removed)


def _daily_date(path: Path, timezone: tzinfo) -> date | None:
    match = _DAILY.fullmatch(path.name)
    if match is None or not path.is_file() or path.stat().st_size == 0:
        return None
    try:
        stamp = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return stamp.astimezone(timezone).date()


def ensure_daily_snapshot(
    database: Path, backup_dir: Path, *, keep: int, now: datetime, timezone: tzinfo
) -> Path:
    """返回配置时区当天的自动快照；目录锁让全部备份入口串行执行。

    最终文件名只在 VACUUM 成功后原子出现；临时文件不视为备份成功。
    已有当天快照仍执行保留清理，让前次清理失败可以在下一轮恢复。
    """
    if keep < 1:
        raise ValueError("keep 至少为 1")
    day = now.astimezone(timezone).date()
    with _backup_lock(backup_dir):
        existing = [
            path
            for path in backup_dir.iterdir()
            if path.is_file() and not path.samefile(database) and _daily_date(path, timezone) == day
        ]
        if existing:
            selected = max(existing, key=lambda path: (_stamp(path), path.stat().st_mtime_ns))
            _prune(backup_dir, keep, database=database, preserve=selected)
            return selected
        if not database.is_file():
            raise LitGleanerError(f"备份失败：数据库不存在：{database}")
        return _create_snapshot(database, backup_dir, keep=keep, now=now, label="daily").path


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name != ? ORDER BY name",
            (_VERSION_TABLE,),
        )
    ]
    return {
        name: int(connection.execute(f"SELECT count(*) FROM {_quote(name)}").fetchone()[0])
        for name in tables
    }


def _quote(identifier: str) -> str:
    """表名来自 sqlite_master。SQLite 不支持绑定表名，按标识符规则加双引号转义。"""
    return '"' + identifier.replace('"', '""') + '"'


def _fts_problems(connection: sqlite3.Connection, counts: Mapping[str, int]) -> list[str]:
    if "memory_fts" not in counts:
        return []
    try:
        connection.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
    except sqlite3.DatabaseError as error:
        return [f"全文索引完整性检查未通过：{error}"]
    return []


def _inspect(path: Path) -> tuple[str | None, dict[str, int], list[str]]:
    connection = sqlite3.connect(path)
    try:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        problems = [] if integrity == ["ok"] else [f"完整性检查未通过：{'; '.join(integrity)}"]
        counts = _row_counts(connection)
        problems.extend(_fts_problems(connection, counts))
    finally:
        connection.close()
    return current_revision(_sqlite_url(path)), counts, problems


def _upgrade_problems(path: Path, before: Mapping[str, int]) -> list[str]:
    try:
        upgrade_database(_sqlite_url(path))
    except Exception as error:  # 演练要报告任何迁移失败，而不是中断
        return [f"升级到最新版本失败：{error}"]
    _, after, problems = _inspect(path)
    shadows = FTS_TABLES - {"memory_fts"}
    shrunk = sorted(
        name for name, count in before.items() if name not in shadows and after.get(name, 0) < count
    )
    if shrunk:
        problems.append(f"升级后行数减少：{', '.join(shrunk)}")
    return problems


def _check_copy(path: Path) -> SnapshotCheck:
    try:
        revision, counts, problems = _inspect(path)
    except sqlite3.DatabaseError as error:
        return SnapshotCheck(None, False, problems=(f"无法作为数据库打开：{error}",))
    if revision is None:
        problems.append("快照里没有迁移版本记录，不是拾穗的数据库")
    elif revision not in known_revisions():
        problems.append(f"迁移版本 {revision} 不在当前程序的迁移记录里，可能来自更新的版本")
    needs_upgrade = revision != head_revision()
    if not problems and needs_upgrade:
        problems.extend(_upgrade_problems(path, counts))
    return SnapshotCheck(revision, needs_upgrade, counts, tuple(problems))


def check_snapshot(snapshot: Path) -> SnapshotCheck:
    """在临时目录里还原快照：完整性检查、迁移版本、各表行数，旧版本还要能升级到最新。"""
    with tempfile.TemporaryDirectory(prefix="litgleaner-restore-") as directory:
        copy = Path(directory) / "litgleaner.db"
        try:
            shutil.copyfile(snapshot, copy)
        except OSError as error:
            return SnapshotCheck(None, False, problems=(f"无法读取快照：{error}",))
        return _check_copy(copy)


def _has_tables(database: Path) -> bool:
    if not database.is_file():
        return False
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone()
    finally:
        connection.close()
    return row is not None


def upgrade_with_backup(
    database: Path, backup_dir: Path, *, keep: int, now: datetime
) -> Path | None:
    """已有数据且需要迁移时，先备份再升级；备份失败就不迁移。返回快照路径。"""
    url = _sqlite_url(database)
    snapshot: Path | None = None
    if _has_tables(database):
        revision = current_revision(url)
        if revision != head_revision():
            label = f"pre-upgrade-{revision or 'unversioned'}"
            snapshot = create_snapshot(database, backup_dir, keep=keep, now=now, label=label).path
    upgrade_database(url)
    return snapshot
