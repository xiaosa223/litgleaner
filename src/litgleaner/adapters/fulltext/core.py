"""CORE 收录的开放获取全文。

按 DOI 检索对应的 work（文档里的字段查询 doi:"..."），核对 DOI 一致后，依次尝试 downloadUrl
和 links 里类型为 download 的链接，交给 PdfDownloader 下载。CORE 只收录机构知识库和开放期刊
公开的文件，所以记为 open。

按 CORE 的 PDF 下载政策，只用 API 返回的 downloadUrl，不绕过 API 批量抓取它的文件服务器。
文档里的 /discover 也能按 DOI 找全文链接，但它是 POST 接口，共享 HTTP 客户端只支持 GET，
所以用检索接口。需要 API key：未登录用户拿不到全文链接。
文档：https://api.core.ac.uk/docs/v3
"""

from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import Field

from litgleaner.adapters.common.download import PdfDownloader, Resolver, system_resolver
from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.options import FullTextOptions, parse_options
from litgleaner.adapters.common.records import alias_value
from litgleaner.domain.errors import AuthFailed, SchemaChanged
from litgleaner.domain.identity import normalize_doi
from litgleaner.ports.fulltext import FullText, FullTextRequest

_MISSING_KEY = "CORE 需要 API key：请免费注册（https://core.ac.uk/services/api）并设置 CORE_API_KEY"


class CoreFullTextOptions(FullTextOptions):
    base_url: str = "https://api.core.ac.uk/v3"
    min_interval_seconds: float = Field(default=3.0, ge=0)
    max_links: int = Field(default=3, gt=0, le=10)


class CoreFullText:
    name = "core"

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._options = parse_options(CoreFullTextOptions, options, self.name)
        self._has_key = bool(api_key)
        self._http = HttpClient(
            "CORE",
            keep_raw=False,
            base_url=self._options.base_url,
            contact_email=contact_email,
            secrets=[api_key],
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
        doi = normalize_doi(alias_value(request.aliases, "doi"))
        if doi is None:
            return None
        if not self._has_key:
            raise AuthFailed(_MISSING_KEY)
        quoted = '"' + doi.replace('"', "") + '"'
        body = self._http.get_json("/search/works/", {"q": f"doi:{quoted}", "limit": "5"})
        results = body.get("results") if isinstance(body, Mapping) else None
        if not isinstance(results, list):
            raise SchemaChanged("CORE 返回缺少 results 列表")
        matches = [
            work
            for work in results
            if isinstance(work, Mapping) and normalize_doi(str(work.get("doi") or "")) == doi
        ]
        for url in _pdf_links(matches)[: self._options.max_links]:
            content = self._download.fetch(url)
            if content is not None:
                return FullText(content, "open", self.name)
        return None


def _pdf_links(works: list[Mapping[str, Any]]) -> list[str]:
    urls: list[str] = []
    for work in works:
        links = [link for link in work.get("links") or [] if isinstance(link, Mapping)]
        candidates = [
            work.get("downloadUrl"),
            *(link.get("url") for link in links if link.get("type") == "download"),
        ]
        for url in candidates:
            if isinstance(url, str) and url.strip() and url.strip() not in urls:
                urls.append(url.strip())
    return urls
