"""OpenAlex 日常列表采集；引用证据只与当前 cites OR 分组求交。"""

import re
from collections.abc import Callable, Mapping
from typing import Any

from litgleaner.adapters.common.records import RecordBuilder
from litgleaner.adapters.common.window import Window
from litgleaner.adapters.sources.daily_citations import DailyCitations
from litgleaner.domain.identity import Alias

Page = Callable[[Mapping[str, str], int], tuple[list[Mapping[str, Any]], str]]
Fields = Callable[[Mapping[str, Any]], dict[str, Any]]
Seeds = dict[str, tuple[Alias, ...]]


class OpenAlexDaily:
    def __init__(
        self,
        builder: RecordBuilder,
        window: Window,
        seeds: Seeds,
        *,
        limit: int,
        per_page: int,
        select: str,
        keyed: bool,
        page: Page,
        fields: Fields,
    ) -> None:
        self.builder, self.window, self.seeds = builder, window, seeds
        self.limit, self.per_page, self.select = limit, per_page, select
        self.keyed, self.page, self.fields = keyed, page, fields
        self.evidence = DailyCitations()
        self.seen = 0

    def collect(self, base: Mapping[str, str]) -> None:
        """raw 条数跨页、跨 OR 分组共用；尾页先切配额再写入记录和证据。"""
        dates = (
            f"from_publication_date:{self.window.since.isoformat()},"
            f"to_publication_date:{self.window.until.isoformat()}"
        )
        seed_ids = base.get("filter", "").removeprefix("cites:").split("|")
        seeds = {wid: self.seeds[wid] for wid in seed_ids if wid in self.seeds}
        params = {
            **base,
            "filter": f"{base['filter']},{dates}" if "filter" in base else dates,
            "sort": "publication_date:desc",
            "per_page": str(self.per_page),
            "select": f"{self.select},referenced_works" if seeds else self.select,
        }
        page, cursor = 1, "*"
        while True:
            paging = {"cursor": cursor} if self.keyed else {"page": str(page)}
            items, more = self.page({**params, **paging}, page)
            for position, item in enumerate(items[: max(self.limit - self.seen, 0)], self.seen):
                self._add(item, seeds, position)
            self.seen += len(items)
            if self.seen >= self.limit:
                self.builder.truncate(self.limit)
                return
            if not items or not more:
                return
            page, cursor = page + 1, more

    def _add(self, item: Mapping[str, Any], seeds: Seeds, position: int) -> None:
        if not seeds:
            self.builder.add(**self.fields(item))
            return
        record = self.evidence.add_citing(self.builder, self.fields(item))
        if record is None or record.published_at is None:
            return
        if not self.window.since <= record.published_at <= self.window.until:
            return
        for work_id in _cited_ids(item, seeds):
            self.evidence.add(record, seeds[work_id], position, item)


def _cited_ids(work: Mapping[str, Any], seeds: Seeds) -> list[str]:
    if "referenced_works" not in work:
        return list(seeds) if len(seeds) == 1 else []
    references = work["referenced_works"]
    if not isinstance(references, list):
        return []
    ids = [work_id(value) for value in references]
    if any(value is None for value in ids):
        return []
    return [seed for seed in seeds if seed in ids]


def work_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    tail = value.rstrip("/").rsplit("/", 1)[-1]
    return tail if re.fullmatch(r"W[0-9]+", tail) else None
