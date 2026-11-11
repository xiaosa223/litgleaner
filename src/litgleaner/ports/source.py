"""信号源接口。适配器不访问数据库，也不认识主题。"""

from dataclasses import dataclass
from typing import Protocol

from litgleaner.domain.citation import ReferenceRecord
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import Query


@dataclass(frozen=True)
class FetchResult:
    records: tuple[SourceRecord, ...]
    next_cursor: str | None
    warnings: tuple[str, ...] = ()
    raw: bytes | None = None
    # 达到单次上限、窗口没取完。发现阶段据此不推进游标，下次重新抓取这个窗口。
    truncated: bool = False
    references: tuple[ReferenceRecord, ...] = ()


class Source(Protocol):
    name: str
    capabilities: frozenset[type]

    def fetch(self, query: Query, cursor: str | None) -> FetchResult: ...
