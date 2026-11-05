"""OpenAlex 记录的开放获取 PDF 链接。

先查单篇记录（免费，不占每日额度），取 best_oa_location 的 pdf_url，再依次试其他标为开放获取的
位置。只下载这些公开链接，所以记为 open；不使用 OpenAlex 收费的 PDF 缓存服务。
支持按 DOI、arXiv 编号（OpenAlex 用 10.48550/arXiv.* 这个 DOI 收录）、PMID、PMCID 查找。
文档：https://help.openalex.org/api/get-single-entities/
"""

from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import Field

from litfactory.adapters.common.download import PdfDownloader, Resolver, system_resolver
from litfactory.adapters.common.http import HttpClient
from litfactory.adapters.common.options import FullTextOptions, parse_options
from litfactory.adapters.common.records import alias_value
from litfactory.domain.errors import SchemaChanged
from litfactory.domain.identity import Alias
from litfactory.ports.fulltext import FullText, FullTextRequest

_SELECT = "id,best_oa_location,locations"


class OpenAlexFullTextOptions(FullTextOptions):
    base_url: str = "https://api.openalex.org"
    max_links: int = Field(default=3, gt=0, le=10)
    min_interval_seconds: float = Field(default=0.2, ge=0)


class OpenAlexFullText:
    name = "openalex"

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._options = parse_options(OpenAlexFullTextOptions, options, self.name)
        self._contact = contact_email
        self._http = HttpClient(
            "OpenAlex",
            keep_raw=False,
            base_url=self._options.base_url,
            contact_email=contact_email,
            secrets=(api_key,),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
            min_interval=self._options.min_interval_seconds,
            timeout=self._options.timeout_seconds,
            transport=transport,
        )
        self._download = PdfDownloader(
            allow_fake_ip=self._options.allow_proxy_fake_ip,
            max_bytes=self._options.max_pdf_mb * 1_000_000,
            transport=transport,
            resolver=resolver,
        )

    def fetch(self, request: FullTextRequest) -> FullText | None:
        path = _lookup_path(request.aliases)
        if path is None:
            return None
        params = {"select": _SELECT}
        if self._contact:
            params["mailto"] = self._contact
        body = self._http.get_json(path, params, allow_missing=True)
        if body is None:
            return None
        if not isinstance(body, Mapping):
            raise SchemaChanged("OpenAlex 单篇查询返回的不是对象")
        for url in _pdf_links(body)[: self._options.max_links]:
            content = self._download.fetch(url)
            if content is not None:
                return FullText(content, "open", self.name)
        return None


def _lookup_path(aliases: Iterable[Alias]) -> str | None:
    aliases = tuple(aliases)
    doi = alias_value(aliases, "doi")
    if doi:
        return f"/works/doi:{quote(doi, safe='/')}"
    arxiv = alias_value(aliases, "arxiv")
    if arxiv:
        return f"/works/doi:10.48550/arXiv.{quote(arxiv, safe='')}"
    for scheme in ("pmid", "pmcid"):
        value = alias_value(aliases, scheme)
        if value:
            return f"/works/{scheme}:{quote(value, safe='')}"
    return None


def _pdf_links(work: Mapping[str, Any]) -> list[str]:
    candidates: list[Any] = [work.get("best_oa_location")]
    locations = work.get("locations")
    if isinstance(locations, list):
        candidates.extend(loc for loc in locations if isinstance(loc, Mapping) and loc.get("is_oa"))
    urls: list[str] = []
    for location in candidates:
        url = location.get("pdf_url") if isinstance(location, Mapping) else None
        if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    return urls
