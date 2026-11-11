"""全部备份入口共享目录锁，阻止发布和保留清理相互交错。"""

import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from tests.integration.test_daily_backup import NOW, daily, make_database

from litgleaner.infra.db import backup
from litgleaner.infra.db.backup import check_snapshot, create_snapshot


@pytest.fixture
def database(tmp_path: Path) -> Path:
    return make_database(tmp_path)


def test_manual_cannot_publish_or_prune_until_daily_finishes(database, tmp_path, monkeypatch):
    directory = tmp_path / "b"
    pruning, release, started, publishing = (threading.Event() for _ in range(4))
    prune, vacuum = backup._prune, backup._vacuum_into

    def gated_prune(path, keep, *, database, preserve=None):
        if "daily" in preserve.name:
            pruning.set()
            assert release.wait(5)
        return prune(path, keep, database=database, preserve=preserve)

    def observed_vacuum(source, target):
        if "daily" not in target.name:
            publishing.set()
        return vacuum(source, target)

    def manual():
        started.set()
        return create_snapshot(database, directory, keep=1, now=NOW)

    monkeypatch.setattr(backup, "_prune", gated_prune)
    monkeypatch.setattr(backup, "_vacuum_into", observed_vacuum)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(daily, database, directory, keep=1)
        try:
            assert pruning.wait(5)
            second = pool.submit(manual)
            assert started.wait(5)
            assert not publishing.wait(0.2)
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    snapshots = list(directory.glob("*.db"))
    assert len(snapshots) == 1 and check_snapshot(snapshots[0]).ok


def _daily_process(database, directory, first, signals, results):
    original = backup._vacuum_into

    def vacuum(source, target):
        if first:
            signals["pruning"].set()
            assert signals["release"].wait(10)
        else:
            signals["publishing"].set()
        return original(source, target)

    backup._vacuum_into = vacuum
    if not first:
        signals["started"].set()
    try:
        results.put(("ok", str(daily(Path(database), Path(directory)))))
    except BaseException as error:
        results.put(("error", repr(error)))


def test_same_second_daily_calls_are_serialized_between_processes(database, tmp_path):
    context, directory = multiprocessing.get_context("spawn"), tmp_path / "b"
    signals = {name: context.Event() for name in ("pruning", "release", "started", "publishing")}
    results = context.Queue()
    workers = [
        context.Process(
            target=_daily_process, args=(str(database), str(directory), first, signals, results)
        )
        for first in (True, False)
    ]
    try:
        workers[0].start()
        assert signals["pruning"].wait(10)
        workers[1].start()
        assert signals["started"].wait(10)
        assert not signals["publishing"].wait(0.2)
        signals["release"].set()
        for worker in workers:
            worker.join(10)
        returned = [results.get(timeout=2) for _ in workers]
        assert all(worker.exitcode == 0 for worker in workers)
        assert all(status == "ok" for status, _ in returned)
        assert returned[0][1] == returned[1][1]
    finally:
        signals["release"].set()
        for worker in workers:
            if worker.pid is not None:
                worker.join(10)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
        results.close()
    snapshots = list(directory.glob("*.db"))
    assert len(snapshots) == 1 and check_snapshot(snapshots[0]).ok
    assert not list(directory.glob("*.partial"))
