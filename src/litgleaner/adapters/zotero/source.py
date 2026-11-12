"""Zotero 收件箱：用户新加进 Zotero 的论文自动进入流程（只支持 InboxDrain）。

local 模式游标是已处理的最大 dateAdded（Zotero 存的 UTC 时间，"YYYY-MM-DD HH:MM:SS"）。
web 模式游标是库版本号（Last-Modified-Version），之后用 since=版本号 增量读取；
注意 since 返回的是“新增或修改过”的条目，旧条目被编辑后会再次出现，由去重阶段识别。
首次运行（没有游标）只取最近 lookback_days 天加入的条目；lookback_days 为 None 时导入全部历史，
此时按加入时间（local）或版本号（web）从旧到新分批，每次最多 max_records 条。
已知限制：local 模式下，从其他设备同步过来、dateAdded 早于游标的条目不会被读到。
"""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any

import httpx

from litgleaner.adapters.common.capture import captured_today
from litgleaner.adapters.common.options import Today, parse_options, utc_today
from litgleaner.adapters.common.records import RecordBuilder, ensure_supported
from litgleaner.adapters.zotero.discovery import discover_data_dir
from litgleaner.adapters.zotero.library import LocalLibrary
from litgleaner.adapters.zotero.mapping import add_item, is_regular
from litgleaner.adapters.zotero.options import ZoteroSourceOptions, require_api_key
from litgleaner.adapters.zotero.web import Item, ZoteroWebApi
from litgleaner.domain.query import InboxDrain, Query
from litgleaner.ports.source import FetchResult

_LOCAL_CURSOR = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def cut_at_boundary[T](rows: Sequence[T], limit: int, key: Callable[[T], Any]) -> list[T]:
    """取前 limit 条，但不把同一个排序值的条目拆到两次运行里，否则游标会跳过剩下的几条。"""
    if len(rows) <= limit:
        return list(rows)
    boundary = key(rows[limit])
    kept = [row for row in rows[:limit] if key(row) < boundary]
    return kept or list(rows[:limit])


class ZoteroSource:
    capabilities: frozenset[type] = frozenset({InboxDrain})

    def __init__(
        self,
        name: str,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        today: Today = utc_today,
    ) -> None:
        self.name = name
        self._options = parse_options(ZoteroSourceOptions, options, name)
        self._today = captured_today(today)
        self._web: ZoteroWebApi | None = None
        self._local: LocalLibrary | None = None
        key = require_api_key(self._options, api_key, name)
        if key is not None:
            self._web = ZoteroWebApi(
                self._options,
                api_key=key,
                contact_email=contact_email,
                min_interval=self._options.min_interval_seconds,
                timeout=self._options.timeout_seconds,
                transport=transport,
            )
        else:
            self._local = LocalLibrary(discover_data_dir(self._options.data_dir))

    def fetch(self, query: Query, cursor: str | None) -> FetchResult:
        ensure_supported(self.capabilities, query)
        builder = RecordBuilder(self.name)
        if self._web is not None:
            return self._fetch_web(self._web, builder, cursor)
        assert self._local is not None
        return self._fetch_local(self._local, builder, cursor)

    def _limit_note(self, builder: RecordBuilder) -> None:
        builder.note(f"达到单次上限 {self._options.max_records} 条，剩余的下次再取")

    def _cutoff(self) -> str | None:
        days = self._options.lookback_days
        return None if days is None else (self._today() - timedelta(days=days)).isoformat()

    # ---- local ----

    def _fetch_local(
        self, library: LocalLibrary, builder: RecordBuilder, cursor: str | None
    ) -> FetchResult:
        limit = self._options.max_records
        cutoff = self._cutoff()
        start = cursor if cursor and _LOCAL_CURSOR.match(cursor) else None
        after = start or (f"{cutoff} 00:00:00" if cutoff else "")
        rows = library.recent_items(
            after=after, limit=limit + 1, collection=self._options.collection
        )
        kept = cut_at_boundary(rows, limit, key=lambda row: str(row["dateAdded"]))
        if len(rows) > limit:
            self._limit_note(builder)
        for item in kept:
            add_item(builder, item, f"{item['libraryID']}/{item['key']}")
        raw = [{k: v for k, v in item.items() if k != "itemID"} for item in kept]
        return FetchResult(
            records=builder.records,
            next_cursor=str(kept[-1]["dateAdded"]) if kept else (after or None),
            warnings=builder.warnings,
            raw=json.dumps(raw, ensure_ascii=False).encode("utf-8"),
        )

    # ---- web ----

    def _fetch_web(
        self, api: ZoteroWebApi, builder: RecordBuilder, cursor: str | None
    ) -> FetchResult:
        api.reset_raw_log()
        collection = self._options.collection
        scope = api.collection_key(collection) if collection else None
        since = int(cursor) if cursor and _is_number(cursor) else None
        cutoff = self._cutoff()
        if since is None and cutoff is not None:
            items, version = self._web_recent(api, scope, cutoff, builder)
        else:
            items, version = self._web_changed(api, scope, since or 0, builder)
        for item in items:
            meta = item.get("meta")
            parsed = meta.get("parsedDate") if isinstance(meta, Mapping) else None
            zotero_id = f"{api.library_path}/{item['key']}"
            add_item(builder, item["data"], zotero_id, parsed if isinstance(parsed, str) else None)
        return FetchResult(
            records=builder.records,
            next_cursor=version,
            warnings=builder.warnings,
            raw=api.raw_log(),
        )

    def _web_recent(
        self, api: ZoteroWebApi, scope: str | None, cutoff: str, builder: RecordBuilder
    ) -> tuple[list[Item], str]:
        """首次运行：按加入时间从新到旧翻页，遇到早于 cutoff 的条目就停。"""
        size, limit = self._options.page_size, self._options.max_records
        items: list[Item] = []
        start, version = 0, None
        while True:
            page, page_version = api.top_items(scope, start=start, limit=size)
            version = version or page_version
            fresh = [i for i in page if str(i["data"].get("dateAdded", "")) >= cutoff]
            items += [i for i in fresh if is_regular(i["data"])]
            done = len(fresh) < len(page) or len(page) < size
            if len(items) > limit or (len(items) == limit and not done):
                builder.note(f"首次导入达到上限 {limit} 条，只取最新的 {limit} 条，更早的已跳过")
                return items[:limit], version
            if done:
                return items, version
            start += len(page)

    def _web_changed(
        self, api: ZoteroWebApi, scope: str | None, since: int, builder: RecordBuilder
    ) -> tuple[list[Item], str]:
        """增量：先取变更条目的版本号，按版本从旧到新取 max_records 条，游标停在已处理处。"""
        versions, version = api.versions(scope, since)
        ordered = sorted(versions.items(), key=lambda pair: (pair[1], pair[0]))
        kept = cut_at_boundary(ordered, self._options.max_records, key=lambda pair: pair[1])
        if len(ordered) > len(kept):
            self._limit_note(builder)
            version = str(kept[-1][1])
        items = api.items_by_keys([key for key, _ in kept])
        return [item for item in items if is_regular(item["data"])], version


def _is_number(text: str) -> bool:
    """只认 ASCII 数字。str.isdigit() 对“²”之类也返回 True，随后 int() 会抛错。"""
    return text.isascii() and text.isdecimal()
