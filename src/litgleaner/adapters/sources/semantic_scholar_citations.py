"""显式日期范围的被引分页，独立于日常日期游标。

官方：https://api.semanticscholar.org/api-docs/graph#tag/Paper-Data/operation/get_graph_get_paper_citations
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import quote

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.sources.historical_common import Fields, HistoryCollector
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.identity import normalize_doi
from litgleaner.domain.query import HistoricalCitations
from litgleaner.ports.source import FetchResult


@dataclass(frozen=True)
class SemanticScholarCitations:
    http: HttpClient
    collector: HistoryCollector
    field_names: str
    fields: Fields
    page_size: int

    def fetch(self, query: HistoricalCitations) -> FetchResult:
        self.http.reset_raw_log()
        truncated = False
        for doi in sorted({normalize_doi(value) or value for value in query.dois}):
            if self.collector.full:
                truncated = True
                break
            truncated |= self._pages(query, doi)
        return self.collector.result(self.http.raw_log(), truncated=truncated)

    def _pages(self, query: HistoricalCitations, doi: str) -> bool:
        offset = 0
        while True:
            room = self.collector.limit - self.collector.seen
            body = self.http.get_json(
                f"/paper/{quote(f'DOI:{doi}', safe='/:')}/citations",
                {
                    "fields": self.field_names,
                    "publicationDateOrYear": f"{query.since.isoformat()}:{query.until.isoformat()}",
                    "offset": str(offset),
                    "limit": str(min(room, self.page_size)),
                },
                allow_missing=offset == 0,
            )
            if body is None:
                self.collector.builder.note(f"种子 DOI {doi} 在来源中找不到，未覆盖被引查询")
                return False
            items, following = _page(body, offset)
            papers = [
                item.get("citingPaper") if isinstance(item, Mapping) else None for item in items
            ]
            self.collector.consume(papers, self._fields)
            if self.collector.full or following is None:
                return len(items) > room or following is not None
            offset = following

    def _fields(self, item: Mapping[str, Any]) -> dict[str, Any]:
        data = self.fields(item)
        if not item.get("publicationDate") and isinstance(data.get("published_at"), date):
            self.collector.year_only += 1
        return data


def _page(body: Any, offset: int) -> tuple[list[Any], int | None]:
    items = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(items, list):
        raise SchemaChanged("历史被引查询缺少 data 列表")
    following, actual = body.get("next"), body.get("offset", offset)
    if type(actual) is not int or actual != offset:
        raise SchemaChanged("历史被引查询 offset 与请求不一致")
    if following is not None and (type(following) is not int or following <= offset or not items):
        raise SchemaChanged("历史被引查询 next 无效或重复")
    return items, following
