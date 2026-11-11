"""参考文献用 /references 按 offset 分页；不沿用新文章的日期窗口。"""

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

AddPaper = Callable[[RecordBuilder, Mapping[str, Any]], None]


class SemanticScholarReferences:
    def __init__(
        self,
        http: HttpClient,
        builder: RecordBuilder,
        *,
        limit: int,
        page_size: int,
        fields: str,
        add_paper: AddPaper,
    ) -> None:
        self.http, self.builder = http, builder
        self.limit, self.page_size = limit, page_size
        # 官方语法：目标论文的字段直接列出，不加 citedPaper. 前缀。
        self.fields = f"contexts,intents,isInfluential,{fields}"
        self.add_paper = add_paper

    def fetch(self, query: ReferenceList) -> FetchResult:
        self.http.reset_raw_log()
        references: list[ReferenceRecord] = []
        truncated = False
        for doi in dict.fromkeys(query.dois):
            if len(references) >= self.limit:
                truncated = True
                break
            truncated |= self._pages(doi, references)
        return reference_result(
            self.http, self.builder, references, truncated=truncated, limit=self.limit
        )

    def _pages(self, doi: str, references: list[ReferenceRecord]) -> bool:
        path = f"/paper/{quote(f'DOI:{doi.strip()}', safe='/:')}/references"
        offset = 0
        while True:
            room = self.limit - len(references)
            body = self.http.get_json(
                path,
                {
                    "fields": self.fields,
                    "offset": str(offset),
                    "limit": str(min(room, self.page_size)),
                },
                allow_missing=offset == 0,
            )
            if body is None:
                self.builder.note(f"Semantic Scholar 找不到 DOI {doi}，已跳过")
                return False
            items, following = _page(body, offset)
            for index, item in enumerate(items[:room]):
                references.append(self._reference(doi, offset + index, item))
            if len(items) > room or (following is not None and len(references) >= self.limit):
                return True
            if following is None:
                return False
            if not items:
                raise SchemaChanged("Semantic Scholar 空参考文献页仍包含 next")
            offset = following

    def _reference(self, doi: str, position: int, item: Mapping[str, Any]) -> ReferenceRecord:
        paper = item.get("citedPaper")
        if "citedPaper" not in item or (paper is not None and not isinstance(paper, Mapping)):
            raise SchemaChanged("Semantic Scholar 参考文献缺少 citedPaper 对象或 null")
        title = None
        aliases: tuple[Alias, ...] = ()
        if paper is not None:
            title = reference_title(paper)
            aliases = _aliases(paper)
            try:
                self.add_paper(self.builder, paper)
            except (TypeError, ValueError, AttributeError) as error:
                raise SchemaChanged("Semantic Scholar 参考文献字段结构异常") from error
        influential = item.get("isInfluential")
        if influential is not None and not isinstance(influential, bool):
            raise SchemaChanged("Semantic Scholar isInfluential 不是布尔值")
        return ReferenceRecord(
            citing_doi=doi,
            position=position,
            raw_text=raw_reference(item),
            cited_title=title,
            cited_aliases=aliases,
            contexts=_strings(item, "contexts"),
            intents=_strings(item, "intents"),
            influential=influential,
        )


def _page(body: Any, offset: int) -> tuple[list[Mapping[str, Any]], int | None]:
    items = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
        raise SchemaChanged("Semantic Scholar 参考文献 data 不是对象列表")
    following = body.get("next")
    if following is not None and (type(following) is not int or following <= offset):
        raise SchemaChanged("Semantic Scholar 参考文献 next 无效")
    actual = body.get("offset", offset)
    if type(actual) is not int or actual != offset:
        raise SchemaChanged("Semantic Scholar 参考文献 offset 与请求不符")
    return items, following


def _strings(item: Mapping[str, Any], key: str) -> tuple[str, ...]:
    values = item.get(key)
    values = [] if values is None else values
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise SchemaChanged(f"Semantic Scholar 参考文献 {key} 不是字符串列表")
    return tuple(values)


def _aliases(paper: Mapping[str, Any]) -> tuple[Alias, ...]:
    ids = paper.get("externalIds")
    ids = {} if ids is None else ids
    if not isinstance(ids, Mapping):
        raise SchemaChanged("Semantic Scholar externalIds 不是对象")
    external = {"s2": paper.get("paperId")}
    if external["s2"] is not None and not isinstance(external["s2"], str):
        raise SchemaChanged("Semantic Scholar paperId 不是字符串")
    for key, value in ids.items():
        if not isinstance(key, str) or (value is not None and type(value) not in {str, int}):
            raise SchemaChanged("Semantic Scholar externalIds 编号结构异常")
        if key in {"DOI", "ArXiv"} and value is not None and not isinstance(value, str):
            raise SchemaChanged("Semantic Scholar DOI/ArXiv 不是字符串")
        if key not in {"DOI", "ArXiv"} and value is not None:
            scheme = {"PubMed": "pmid", "PubMedCentral": "pmcid"}.get(key, key.lower())
            text = str(value)
            external[scheme] = (
                f"PMC{text}" if scheme == "pmcid" and not text.upper().startswith("PMC") else text
            )
    return aliases_from_fields(
        {"doi": ids.get("DOI"), "arxiv_id": ids.get("ArXiv"), "external_ids": external}
    )
