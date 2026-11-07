"""SQLite 连接和事务。atomic 内的所有操作共用一个事务，可以嵌套。"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Executable, Row, create_engine, event
from sqlalchemy.engine import Engine, make_url


def ensure_parent_dir(url: str) -> None:
    database = make_url(url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)


def _set_pragmas(connection: Any, _record: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Database:
    def __init__(self, url: str) -> None:
        ensure_parent_dir(url)
        self._engine: Engine = create_engine(url)
        event.listen(self._engine, "connect", _set_pragmas)
        self._active: Connection | None = None

    def dispose(self) -> None:
        self._engine.dispose()

    @contextmanager
    def atomic(self) -> Iterator[None]:
        if self._active is not None:
            yield
            return
        with self._engine.begin() as connection:
            self._active = connection
            try:
                yield
            finally:
                self._active = None

    def fetch(self, statement: Executable) -> Sequence[Row[Any]]:
        if self._active is not None:
            return self._active.execute(statement).all()
        with self._engine.begin() as connection:
            return connection.execute(statement).all()

    def write(self, statement: Executable) -> int:
        """返回受影响行数。"""
        if self._active is not None:
            return self._active.execute(statement).rowcount
        with self._engine.begin() as connection:
            return connection.execute(statement).rowcount
