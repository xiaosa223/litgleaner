"""迁移和事务。"""

import sqlite3
import threading
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, insert, inspect, select

from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import Paper
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config, is_up_to_date, upgrade_database
from litgleaner.infra.db.schema import metadata, source_cursor
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import JobSpec


@pytest.fixture
def url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'nested' / '中文路径' / 'test.db'}"


@pytest.fixture
def store(url: str) -> SqlStore:
    upgrade_database(url)
    return SqlStore(url)


def make_paper(id_: str = "P1") -> Paper:
    return Paper(id_, "T", "t", None, ("A",), None, None, None, "2026-01-01")


def test_migration_matches_schema_exactly(url: str):
    upgrade_database(url)
    engine = create_engine(url)
    with engine.connect() as connection:
        diff = compare_metadata(MigrationContext.configure(connection), metadata)
    engine.dispose()
    assert diff == []


def test_upgrade_is_idempotent_and_detected(url: str):
    assert not is_up_to_date(url)
    upgrade_database(url)
    upgrade_database(url)
    assert is_up_to_date(url)
    engine = create_engine(url)
    assert "paper" in inspect(engine).get_table_names()
    engine.dispose()


def test_atomic_rolls_back_everything_on_error(store: SqlStore):
    with pytest.raises(RuntimeError), store.atomic():
        store.papers.insert(make_paper())
        with store.atomic():
            store.papers.add_aliases("P1", (Alias("doi", "10.1/x"),))
        raise RuntimeError("中途失败")
    assert store.papers.get("P1") is None
    assert store.papers.find_ids_by_aliases((Alias("doi", "10.1/x"),)) == frozenset()


def test_foreign_keys_are_enforced(store: SqlStore):
    with pytest.raises(Exception, match="FOREIGN KEY"):
        store.papers.add_aliases("missing", (Alias("doi", "10.1/x"),))


def test_fill_missing_never_overwrites(store: SqlStore):
    store.papers.insert(make_paper())
    store.papers.fill_missing("P1", {"abstract": "first"})
    store.papers.fill_missing("P1", {"abstract": "second"})
    paper = store.papers.get("P1")
    assert paper is not None
    assert paper.abstract == "first"
    with pytest.raises(ValueError, match="不允许"):
        store.papers.fill_missing("P1", {"title": "x"})


def test_enqueue_is_idempotent_even_after_done(store: SqlStore):
    spec = JobSpec("k", "stage", {"a": 1})
    assert store.jobs.enqueue(spec, "2026-01-01")
    assert not store.jobs.enqueue(spec, "2026-01-02")
    store.jobs.mark_done("k", "2026-01-01")
    assert not store.jobs.enqueue(spec, "2026-01-03")
    assert store.jobs.due("stage", "2027-01-01") == ()


def test_due_respects_backoff_and_retry_dead_resets(store: SqlStore):
    store.jobs.enqueue(JobSpec("k", "stage", {}), "2026-01-01")
    store.jobs.mark_retry("k", "err", "2026-01-05", "2026-01-01")
    assert store.jobs.due("stage", "2026-01-02") == ()
    assert [j.attempts for j in store.jobs.due("stage", "2026-01-06")] == [1]
    store.jobs.mark_dead("k", "err", "2026-01-06")
    assert store.jobs.retry_dead("2026-01-07") == 1
    assert [j.attempts for j in store.jobs.due("stage", "2026-01-07")] == [0]


def test_migration_0004_backfills_provisional_flag_from_cards(tmp_path: Path):
    path = tmp_path / "old.db"
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0003")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO digest VALUES ('D1', 'r', 't', 'me', '2026-11-01', '2026-11-01')"
        )
        for position, provisional in enumerate(("true", "false")):
            connection.execute(
                "INSERT INTO digest_item VALUES ('D1', ?, 'P', ?)",
                (position, f'{{"provisional": {provisional}}}'),
            )
    connection.close()

    upgrade_database(url)

    connection = sqlite3.connect(path)
    rows = connection.execute("SELECT provisional FROM digest_item ORDER BY position").fetchall()
    connection.close()
    assert rows == [(1,), (0,)]


def test_transaction_in_one_thread_is_invisible_to_another(url: str):
    """常驻服务里 HTTP 请求和调度在不同线程。一个线程的事务不能被另一个线程借用。"""
    upgrade_database(url)
    db = Database(url)
    in_transaction = threading.Event()
    checked = threading.Event()
    seen: list[int] = []

    def writer() -> None:
        with pytest.raises(RuntimeError), db.atomic():
            db.write(insert(source_cursor).values(source="s", query_hash="q", updated_at="t"))
            in_transaction.set()
            checked.wait(5)
            raise RuntimeError("回滚")

    thread = threading.Thread(target=writer)
    thread.start()
    assert in_transaction.wait(5)
    seen.append(len(db.fetch(select(source_cursor))))
    checked.set()
    thread.join(5)

    assert seen == [0]
    assert db.fetch(select(source_cursor)) == []
    db.dispose()
