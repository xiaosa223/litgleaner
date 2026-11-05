"""信号源接口。适配器不访问数据库，也不认识主题。"""

from dataclasses import dataclass
from typing import Protocol

from litfactory.domain.paper import SourceRecord
from litfactory.domain.query import Query


@dataclass(frozen=True)
class FetchResult:
    records: tuple[SourceRecord, ...]
    next_cursor: str | None
    warnings: tuple[str, ...] = ()
    raw: bytes | None = None


class Source(Protocol):
    name: str
    capabilities: frozenset[type]

    def fetch(self, query: Query, cursor: str | None) -> FetchResult: ...
