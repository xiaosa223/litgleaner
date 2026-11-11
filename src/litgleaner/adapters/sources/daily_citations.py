"""日常引用追踪的采纳边界和原始证据；不访问网络或推断缺失端点。"""

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from litgleaner.adapters.common.records import RecordBuilder
from litgleaner.domain.citation import SourceCitation
from litgleaner.domain.identity import Alias, record_aliases
from litgleaner.domain.paper import SourceRecord


def _evidence_id(scheme: str, value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    patterns = {"pmid": r"[0-9]+", "pmcid": r"(?i:PMC)[0-9]+", "openalex": r"W[0-9]+"}
    return scheme not in patterns or re.fullmatch(patterns[scheme], value) is not None


class DailyCitations:
    def __init__(self) -> None:
        self.items: list[SourceCitation] = []
        self._records: list[SourceRecord] = []
        self._seen: set[tuple[Any, ...]] = set()

    @property
    def records(self) -> tuple[SourceRecord, ...]:
        return tuple(self._records)

    def add_citing(self, builder: RecordBuilder, fields: Mapping[str, Any]) -> SourceRecord | None:
        """保留原校验、去重和条数，候选与证据都只携带合法类型的标识符。"""
        before = len(builder.records)
        builder.add(**fields)
        ids = {
            key: value
            for key, value in fields.get("external_ids", {}).items()
            if _evidence_id(key, value)
        }
        try:
            record = SourceRecord.model_validate({**fields, "external_ids": ids})
        except ValidationError:
            return None
        if len(builder.records) > before:
            self._records.append(record)
        return record

    def add(
        self,
        record: SourceRecord,
        cited_aliases: tuple[Alias, ...],
        position: int,
        raw: Mapping[str, Any],
    ) -> None:
        citing_aliases = record_aliases(record)
        if not citing_aliases or not cited_aliases:
            return
        influential = raw.get("isInfluential")
        edge = SourceCitation(
            citing_aliases=citing_aliases,
            cited_aliases=cited_aliases,
            source_position=position,
            raw_text=json.dumps(raw, ensure_ascii=False, sort_keys=True),
            contexts=_strings(raw.get("contexts")),
            intents=_strings(raw.get("intents")),
            influential=influential if isinstance(influential, bool) else None,
        )
        # 分页位置不构成新证据；不同端点或不同上下文则各自保留。
        key = (citing_aliases, cited_aliases, edge.raw_text)
        if key not in self._seen:
            self._seen.add(key)
            self.items.append(edge)


def _strings(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return ()
    return tuple(value)
