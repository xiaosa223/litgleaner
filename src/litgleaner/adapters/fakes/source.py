"""假信号源：从 JSON 文件读取记录，用于测试和演示。"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from litgleaner.adapters.common.capture import read_source_bytes
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import IdentifierLookup, JournalWatch, KeywordSearch, Query
from litgleaner.ports.source import FetchResult


class FakeSource:
    capabilities: frozenset[type] = frozenset({JournalWatch, KeywordSearch, IdentifierLookup})

    def __init__(self, name: str, records_file: Path) -> None:
        self.name = name
        self._file = records_file
        self.fail_next: list[Exception] = []

    def fetch(self, query: Query, cursor: str | None) -> FetchResult:
        if type(query) not in self.capabilities:
            raise ValueError(f"不支持的查询：{type(query).__name__}")
        if self.fail_next:
            raise self.fail_next.pop(0)
        raw = read_source_bytes(self._file.read_bytes)
        items: list[Mapping[str, Any]] = json.loads(raw)
        start = int(cursor) if cursor else 0
        try:
            records = tuple(SourceRecord.model_validate(item) for item in items[start:])
        except ValidationError as error:
            raise SchemaChanged(f"记录不符合结构：{error}") from error
        if isinstance(query, IdentifierLookup):  # 按标识符查：在全部记录里找，不用游标
            wanted = {query.doi, query.arxiv_id} - {None}
            everything = (SourceRecord.model_validate(item) for item in items)
            found = tuple(r for r in everything if wanted & {r.doi, r.arxiv_id})
            return FetchResult(records=found[:1], next_cursor=None, raw=raw)
        return FetchResult(records=records, next_cursor=str(len(items)), raw=raw)
