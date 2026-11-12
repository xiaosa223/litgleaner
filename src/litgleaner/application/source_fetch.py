"""四种业务消费者共用的来源入口；用途只描述原采纳范围。"""

from collections.abc import Mapping, Sequence
from typing import Any

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.domain.query import Query
from litgleaner.ports.source import FetchResult, Source
from litgleaner.ports.source_capture import CapturePurpose, CaptureScope


def scopes(
    purpose: CapturePurpose,
    owner: Mapping[str, Any],
    members: Sequence[tuple[str, Query, str | None]],
) -> tuple[CaptureScope, ...]:
    """冻结整个操作的调用顺序；未调用成员没有虚构的响应凭据。"""
    group_id = str(ULID())
    calls = [
        {
            "source": name,
            "query": query.model_dump(mode="json"),
            "query_hash": query.hash,
            "cursor": cursor,
        }
        for name, query, cursor in members
    ]
    return tuple(
        CaptureScope(
            purpose,
            {
                **owner,
                "scope_version": 1,
                "group_id": group_id,
                "member_index": index,
                "members": calls,
            },
        )
        for index in range(len(calls))
    )


def fetch(
    ctx: AppContext, source: Source, query: Query, cursor: str | None, scope: CaptureScope | None
) -> FetchResult:
    if ctx.source_capture is None:
        return source.fetch(query, cursor)
    if scope is None:
        raise ValueError("来源采集缺少业务范围")
    return ctx.source_capture.fetch(source, query, cursor, scope)
