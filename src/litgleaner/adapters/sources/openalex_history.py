"""显式历史检索：日期闭区间、真实 type:review 与源端排序。

官方：https://help.openalex.org/api/filtering.md 、api/sorting.md 、data/work-types.md。
综述类型来自来源元数据，漏标的综述不会靠标题补齐。
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.query import boolean_query
from litgleaner.adapters.sources.historical_common import Fields, HistoryCollector
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.identity import normalize_doi
from litgleaner.domain.query import HistoricalCitations, HistoricalSearch, KeywordSearch
from litgleaner.ports.source import FetchResult


@dataclass(frozen=True)
class OpenAlexHistory:
    http: HttpClient
    collector: HistoryCollector
    fields: Fields
    params: Mapping[str, str]
    per_page: int
    keyed: bool

    def fetch(self, query: HistoricalSearch | HistoricalCitations) -> FetchResult:
        self.http.reset_raw_log()
        params = self._params(query)
        if params is None:
            return self.collector.result(self.http.raw_log(), truncated=False)
        page, cursor, visited = 1, "*", {"*"}
        while True:
            paging = {"cursor": cursor} if self.keyed else {"page": str(page)}
            body = self.http.get_json("/works", {**params, **paging})
            items, meta = _page(body)
            room = self.collector.limit - self.collector.seen
            self.collector.consume(items, self.fields)
            more = self._more(meta, visited, page, int(params["per_page"]), len(items))
            if self.collector.full or not items or not more:
                return self.collector.result(
                    self.http.raw_log(), truncated=len(items) > room or bool(more)
                )
            page, cursor = page + 1, more
            visited.add(cursor)

    def _params(self, query: HistoricalSearch | HistoricalCitations) -> dict[str, str] | None:
        dates = (
            f"from_publication_date:{query.since.isoformat()},"
            f"to_publication_date:{query.until.isoformat()}"
        )
        params = {
            **self.params,
            "filter": f"{dates},type:review" if self.collector.category == "reviews" else dates,
            "sort": "cited_by_count:desc"
            if self.collector.category == "highly_cited"
            else "publication_date:desc",
            "per_page": str(min(self.per_page, self.collector.limit)),
        }
        if isinstance(query, HistoricalSearch):
            params["search.title_abstract_keywords"] = boolean_query(
                KeywordSearch(terms=query.terms, exclude=query.exclude)
            )
        else:
            seeds = self._seeds(query)
            if not seeds:
                return None
            params["filter"] += f",cites:{'|'.join(seeds)}"
        return params

    def _seeds(self, query: HistoricalCitations) -> list[str]:
        found = []
        for doi in sorted({normalize_doi(value) or value for value in query.dois}):
            body = self.http.get_json(
                f"/works/doi:{quote(doi, safe='/')}",
                {**self.params, "select": "id"},
                allow_missing=True,
            )
            raw = body.get("id") if isinstance(body, Mapping) else None
            if not isinstance(raw, str) or not re.fullmatch(
                r"W\d+", raw.rstrip("/").rsplit("/", 1)[-1]
            ):
                self.collector.builder.note(f"种子 DOI {doi} 没有来源编号，未覆盖被引查询")
            else:
                found.append(raw.rstrip("/").rsplit("/", 1)[-1])
        return found

    def _more(
        self,
        meta: Mapping[str, Any],
        visited: set[str],
        page: int,
        size: int,
        received: int,
    ) -> str:
        if self.keyed:
            following = meta.get("next_cursor")
            if following is None or following == "":
                return ""
            if not isinstance(following, str) or following in visited:
                raise SchemaChanged("OpenAlex 历史查询游标无效或重复")
            if not received:
                raise SchemaChanged("OpenAlex 历史查询空页仍有下一页")
            return following
        count = meta.get("count")
        if count is not None and (type(count) is not int or count < 0):
            raise SchemaChanged("OpenAlex 历史查询 count 不是非负整数")
        more = count > page * size if count is not None else received >= size
        if count is not None and count > (page - 1) * size and not received:
            raise SchemaChanged("OpenAlex 历史查询条目数与空页不一致")
        return "more" if more and received else ""


def _page(body: Any) -> tuple[list[Any], Mapping[str, Any]]:
    meta = body.get("meta") if isinstance(body, Mapping) else None
    items = body.get("results") if isinstance(body, Mapping) else None
    if not isinstance(meta, Mapping) or not isinstance(items, list):
        raise SchemaChanged("OpenAlex 历史查询缺少 meta 或 results 列表")
    return items, meta
