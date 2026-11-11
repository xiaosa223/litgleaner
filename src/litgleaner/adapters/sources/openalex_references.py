"""从种子的 referenced_works 批量读取参考文献，保留缺失的目标及原序号。"""

import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.records import RecordBuilder
from litgleaner.adapters.sources.reference_common import (
    aliases_from_fields,
    raw_reference,
    reference_result,
    reference_title,
)
from litgleaner.domain.citation import ReferenceRecord
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import ReferenceList
from litgleaner.ports.source import FetchResult

WorkFields = Callable[[Mapping[str, Any]], dict[str, Any]]


class OpenAlexReferences:
    def __init__(
        self,
        http: HttpClient,
        builder: RecordBuilder,
        *,
        limit: int,
        batch_size: int,
        params: Mapping[str, str],
        fields: WorkFields,
    ) -> None:
        self.http, self.builder = http, builder
        self.limit, self.batch_size = limit, batch_size
        self.params, self.fields = params, fields

    def fetch(self, query: ReferenceList) -> FetchResult:
        self.http.reset_raw_log()
        references: list[ReferenceRecord] = []
        truncated = False
        for doi in dict.fromkeys(query.dois):
            room = self.limit - len(references)
            if room <= 0:
                truncated = True
                break
            ids = self._seed(doi)
            selected = ids[:room]
            truncated |= len(selected) < len(ids)
            details = self._details(selected)
            for position, raw_id in enumerate(selected):
                work = details.get(_work_id(raw_id))
                references.append(self._reference(doi, position, raw_id, work))
        return reference_result(
            self.http, self.builder, references, truncated=truncated, limit=self.limit
        )

    def _seed(self, doi: str) -> list[str]:
        body = self.http.get_json(
            f"/works/doi:{quote(doi.strip(), safe='/')}",
            {**self.params, "select": "id,referenced_works"},
            allow_missing=True,
        )
        if body is None:
            self.builder.note(f"种子 DOI {doi} 在 OpenAlex 中找不到，已跳过")
            return []
        ids = body.get("referenced_works") if isinstance(body, Mapping) else None
        if not isinstance(ids, list):
            raise SchemaChanged("OpenAlex 种子返回缺少 referenced_works 列表")
        for raw_id in ids:
            _work_id(raw_id)
        return ids

    def _details(self, ids: list[str]) -> dict[str, Mapping[str, Any]]:
        unique = list(dict.fromkeys(_work_id(value) for value in ids))
        details: dict[str, Mapping[str, Any]] = {}
        for start in range(0, len(unique), self.batch_size):
            batch = unique[start : start + self.batch_size]
            body = self.http.get_json(
                "/works",
                {
                    **self.params,
                    "filter": f"openalex:{'|'.join(batch)}",
                    "per_page": str(len(batch)),
                },
            )
            items = body.get("results") if isinstance(body, Mapping) else None
            if not isinstance(items, list):
                raise SchemaChanged("OpenAlex 参考文献详情缺少 results 列表")
            for item in items:
                if not isinstance(item, Mapping):
                    raise SchemaChanged("OpenAlex 参考文献详情不是对象")
                work_id = _work_id(item.get("id"))
                if work_id not in batch:
                    raise SchemaChanged("OpenAlex 参考文献详情包含未请求的编号")
                details[work_id] = item
        return details

    def _reference(
        self, doi: str, position: int, raw_id: str, work: Mapping[str, Any] | None
    ) -> ReferenceRecord:
        aliases: tuple[Alias, ...]
        if work is None:
            self.builder.note(f"参考文献 {_work_id(raw_id)} 缺少详情，已保留原编号")
            aliases, title = (Alias("openalex", _work_id(raw_id)),), None
        else:
            title = reference_title(work)
            try:
                fields = self.fields(work)
                aliases = _aliases(work, fields)
                self.builder.add(**fields)
            except (TypeError, ValueError, AttributeError) as error:
                raise SchemaChanged("OpenAlex 参考文献字段结构异常") from error
        return ReferenceRecord(
            citing_doi=doi,
            position=position,
            raw_text=raw_reference({"referenced_work": raw_id, "work": work}),
            cited_title=title,
            cited_aliases=aliases,
        )


def _aliases(work: Mapping[str, Any], fields: dict[str, Any]) -> tuple[Alias, ...]:
    ids = work.get("ids")
    if ids is not None and not isinstance(ids, Mapping):
        raise SchemaChanged("OpenAlex 参考文献 ids 不是对象")
    external = dict(fields["external_ids"])
    for key, value in (ids or {}).items():
        if not isinstance(key, str) or (value is not None and type(value) not in {str, int}):
            raise SchemaChanged("OpenAlex 参考文献 ids 编号结构异常")
        if key not in {"openalex", "doi", "pmid", "pmcid"}:
            external[key] = value
    doi = fields["doi"] or (ids or {}).get("doi")
    if doi is not None and not isinstance(doi, str):
        raise SchemaChanged("OpenAlex 参考文献 doi 不是字符串")
    return aliases_from_fields({**fields, "doi": doi, "external_ids": external})


def _work_id(value: Any) -> str:
    if not isinstance(value, str):
        raise SchemaChanged("OpenAlex 参考文献编号不是字符串")
    tail = value.rstrip("/").rsplit("/", 1)[-1]
    if not re.fullmatch(r"W[0-9]+", tail):
        raise SchemaChanged("OpenAlex 参考文献编号格式异常")
    return tail
