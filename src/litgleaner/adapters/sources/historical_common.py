"""历史搜索的条目预算与来源元数据校验，不根据标题推断类型。"""

from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from litgleaner.adapters.common.records import RecordBuilder
from litgleaner.domain.query import HistoricalSearch
from litgleaner.ports.source import FetchResult

Fields = Callable[[Mapping[str, Any]], dict[str, Any]]


def citation_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def publication_types(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(dict.fromkeys(item for item in value if isinstance(item, str) and item.strip()))


class HistoryCollector:
    def __init__(
        self, query: HistoricalSearch, builder: RecordBuilder, max_records: int, review_type: str
    ) -> None:
        self.query, self.builder = query, builder
        self.limit, self.max_records = min(query.limit, max_records), max_records
        self.review_type, self.seen = review_type, 0
        self.skipped, self.year_only = 0, 0
        self.unknown_counts, self.unknown_types = 0, 0

    @property
    def full(self) -> bool:
        return self.seen >= self.limit

    def consume(self, items: list[Any], fields: Fields) -> None:
        room = self.limit - self.seen
        selected = items[:room]
        self.seen += len(selected)
        for item in selected:
            if not isinstance(item, Mapping):
                self.skipped += 1
                continue
            data = fields(item)
            published = data.get("published_at")
            valid_date = (
                isinstance(published, date) and self.query.since <= published <= self.query.until
            )
            valid_type = self.query.category != "reviews" or self.review_type in data.get(
                "publication_types", ()
            )
            if not valid_date or not valid_type:
                self.skipped += 1
                continue
            self.unknown_counts += data.get("citation_count") is None
            self.unknown_types += not data.get("publication_types")
            self.builder.add(**data)

    def result(self, raw: bytes, *, truncated: bool) -> FetchResult:
        if self.skipped:
            self.builder.note(
                f"历史查询有 {self.skipped} 条缺少可核实的日期/综述类型或结构无效，"
                "已跳过；来源可能漏标"
            )
        if self.year_only:
            self.builder.note(
                f"{self.year_only} 条仅有年份，按来源规则使用该年 1 月 1 日；日期精度有限"
            )
        if self.unknown_counts and self.query.category == "highly_cited":
            self.builder.note(
                f"{self.unknown_counts} 条来源被引数缺失或无效，保留为未知并排在已知数之后"
            )
        if self.unknown_types:
            self.builder.note(
                f"{self.unknown_types} 条来源出版类型缺失或无效，保留为未知，不按标题补全"
            )
        if truncated:
            boundary = []
            if self.limit == self.query.limit:
                boundary.append(f"HistoricalSearch.limit={self.query.limit}")
            if self.limit == self.max_records:
                boundary.append(f"SourceOptions.max_records={self.max_records}")
            self.builder.note(f"达到历史查询原始条目上限 {' / '.join(boundary)}，结果已截断")
        records = self.builder.records
        if self.query.category == "highly_cited":
            records = tuple(
                sorted(
                    records,
                    key=lambda r: -(r.citation_count if r.citation_count is not None else -1),
                )
            )
        else:
            records = tuple(sorted(records, key=lambda r: r.published_at or date.min, reverse=True))
        return FetchResult(records, None, self.builder.warnings, raw, truncated)
