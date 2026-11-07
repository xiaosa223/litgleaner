"""把外部条目变成 SourceRecord。单条坏数据跳过并告警，不让整批失败。"""

from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import ValidationError

from litgleaner.domain.identity import Alias, normalize_doi
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import Query


def ensure_supported(capabilities: frozenset[type], query: Query) -> None:
    if type(query) not in capabilities:
        raise ValueError(f"不支持的查询：{type(query).__name__}")


class RecordBuilder:
    def __init__(self, source: str) -> None:
        self._source = source
        self._records: list[SourceRecord] = []
        self._seen: set[str] = set()
        self._skipped = 0
        self._notes: list[str] = []
        self._truncated = False

    def add(self, *, external_ids: Mapping[str, str | None] | None = None, **fields: Any) -> None:
        ids = {key: str(value) for key, value in (external_ids or {}).items() if value}
        try:
            record = SourceRecord.model_validate({**fields, "external_ids": ids})
        except ValidationError:
            self._skipped += 1
            return
        identity = (
            normalize_doi(record.doi)
            or record.arxiv_id
            or repr(sorted(ids.items()) or record.title)
        )
        if identity in self._seen:
            return
        self._seen.add(identity)
        self._records.append(record)

    def note(self, warning: str) -> None:
        self._notes.append(f"{self._source}：{warning}")

    def truncate(self, limit: int) -> None:
        """达到单次上限。游标不会前进，避免被截掉的记录永久丢失。"""
        self._truncated = True
        self.note(
            f"达到单次上限 {limit} 条，结果已截断；游标不前进，下次重新抓取这个窗口。"
            "如果持续出现，请调大 max_records 或收窄查询"
        )

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def records(self) -> tuple[SourceRecord, ...]:
        return tuple(self._records)

    @property
    def warnings(self) -> tuple[str, ...]:
        skipped = (f"{self._source}：跳过 {self._skipped} 条无效记录",) if self._skipped else ()
        return (*skipped, *self._notes)


def alias_value(aliases: Iterable[Alias], scheme: str) -> str | None:
    """全文来源用：从论文的标识符里取出某一种，例如 doi、arxiv、pmcid。"""
    return next((alias.value for alias in aliases if alias.scheme == scheme), None)
