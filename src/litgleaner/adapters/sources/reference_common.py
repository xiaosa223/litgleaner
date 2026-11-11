"""参考文献查询的事实封装；不创建库内论文，不推断引用关系。"""

import json
from collections.abc import Mapping
from typing import Any

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.records import RecordBuilder
from litgleaner.domain.citation import ReferenceRecord
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.identity import Alias, normalize_arxiv_id, normalize_doi
from litgleaner.ports.source import FetchResult


def raw_reference(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def reference_title(paper: Mapping[str, Any]) -> str | None:
    title = paper.get("title")
    if title is not None and not isinstance(title, str):
        raise SchemaChanged("参考文献的 title 不是字符串")
    return title


def aliases_from_fields(fields: Mapping[str, Any]) -> tuple[Alias, ...]:
    candidates = (
        ("doi", normalize_doi(fields.get("doi"))),
        ("arxiv", normalize_arxiv_id(fields.get("arxiv_id"))),
        *sorted(fields.get("external_ids", {}).items()),
    )
    return tuple(Alias(scheme, str(value)) for scheme, value in candidates if value)


def reference_result(
    http: HttpClient,
    builder: RecordBuilder,
    references: list[ReferenceRecord],
    *,
    truncated: bool,
    limit: int,
) -> FetchResult:
    if truncated:
        builder.note(f"达到单次上限 {limit} 条，参考文献结果已截断")
    return FetchResult(
        records=builder.records,
        next_cursor=None,
        warnings=builder.warnings,
        raw=http.raw_log(),
        truncated=truncated,
        references=tuple(references),
    )
