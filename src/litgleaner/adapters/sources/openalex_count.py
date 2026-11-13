"""OpenAlex 检索词计数：与日常关键词检索同一检索字段，只要 meta.count，每页 1 条、只取 id。

检索请求按次计入 OpenAlex 的用量，所以一组同义词只查一次。
文档：https://help.openalex.org/api/searching.md 、api/paging.md（meta.count）。
"""

from collections.abc import Mapping

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.query import boolean_query
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.query import KeywordSearch, TermCount
from litgleaner.ports.source import FetchResult


def count_works(http: HttpClient, query: TermCount, params: Mapping[str, str]) -> FetchResult:
    http.reset_raw_log()
    body = http.get_json(
        "/works",
        {
            **params,
            "search.title_abstract_keywords": boolean_query(
                KeywordSearch(terms=query.terms, exclude=query.exclude)
            ),
            "filter": (
                f"from_publication_date:{query.since.isoformat()},"
                f"to_publication_date:{query.until.isoformat()}"
            ),
            "per_page": "1",
            "select": "id",
        },
    )
    meta = body.get("meta") if isinstance(body, dict) else None
    total = meta.get("count") if isinstance(meta, dict) else None
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise SchemaChanged("OpenAlex 计数响应缺少 meta.count")
    return FetchResult(records=(), next_cursor=None, raw=http.raw_log(), total=total)
