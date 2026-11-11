"""一次性 bulk 历史查询；官方未提供 batch limit，按原条目本地截断。

官方：https://api.semanticscholar.org/api-docs/graph#tag/Paper-Data/operation/get_graph_get_paper_search_bulk
publicationDateOrYear 将仅有年份的记录按该年 1 月 1 日处理；Review 只认来源类型。
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.sources.historical_common import Fields, HistoryCollector
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.query import HistoricalSearch, KeywordSearch
from litgleaner.ports.source import FetchResult

BulkQuery = Callable[[KeywordSearch], str]


@dataclass(frozen=True)
class SemanticScholarHistory:
    http: HttpClient
    collector: HistoryCollector
    field_names: str
    fields: Fields
    bulk_query: BulkQuery

    def fetch(self, query: HistoricalSearch) -> FetchResult:
        self.http.reset_raw_log()
        params = {
            "query": self.bulk_query(KeywordSearch(terms=query.terms, exclude=query.exclude)),
            "fields": self.field_names,
            "publicationDateOrYear": f"{query.since.isoformat()}:{query.until.isoformat()}",
            "sort": "citationCount:desc"
            if query.category == "highly_cited"
            else "publicationDate:desc",
        }
        if query.category == "reviews":
            params["publicationTypes"] = "Review"
        token: str | None = None
        visited: set[str] = set()
        while True:
            body = self.http.get_json(
                "/paper/search/bulk", {**params, **({"token": token} if token else {})}
            )
            items, following = _page(body, visited)
            room = self.collector.limit - self.collector.seen
            self.collector.consume(items, self._fields)
            if self.collector.full or not items or following is None:
                return self.collector.result(
                    self.http.raw_log(), truncated=len(items) > room or following is not None
                )
            token = following
            visited.add(token)

    def _fields(self, item: Mapping[str, Any]) -> dict[str, Any]:
        data = self.fields(item)
        if not item.get("publicationDate") and isinstance(data.get("published_at"), date):
            self.collector.year_only += 1
        return data


def _page(body: Any, visited: set[str]) -> tuple[list[Any], str | None]:
    items = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(items, list):
        raise SchemaChanged("Semantic Scholar 历史查询缺少 data 列表")
    token = body.get("token")
    if token is not None and (not isinstance(token, str) or token in visited):
        raise SchemaChanged("Semantic Scholar 历史查询 token 无效或重复")
    if token and not items:
        raise SchemaChanged("Semantic Scholar 历史查询空页仍有下一页")
    return items, token or None
