"""Crossref 登记的文本挖掘（TDM）全文链接。

出版商在 Crossref 里登记了 intended-application 为 text-mining 的 PDF 链接。开放获取的文章
任何人都能下载；订阅文章只有在机构网络（校园网、VPN）里运行时出版商才会放行。
许可证是知识共享协议时记为 open，否则一律记为 subscribed，保证这些文件不会被公开。
不带任何出版商专用凭据，也不绕过访问控制：下载被拒就返回 None。
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import Field

from litgleaner.adapters.common.download import PdfDownloader, Resolver, system_resolver
from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.options import FullTextOptions, parse_options
from litgleaner.adapters.common.records import alias_value
from litgleaner.domain.errors import SchemaChanged
from litgleaner.ports.fulltext import FullText, FullTextRequest, LicenseBasis

_APPLICATIONS = frozenset({"text-mining", "similarity-checking", "unspecified"})
_OPEN_LICENSE_MARKERS = ("creativecommons.org", "/cc-by", "open-access")


class CrossrefTdmOptions(FullTextOptions):
    base_url: str = "https://api.crossref.org"
    max_links: int = Field(default=3, gt=0, le=10)


class CrossrefTdmFullText:
    name = "crossref_tdm"

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._options = parse_options(CrossrefTdmOptions, options, self.name)
        self._contact = contact_email
        self._http = HttpClient(
            "Crossref",
            keep_raw=False,
            base_url=self._options.base_url,
            contact_email=contact_email,
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
        doi = alias_value(request.aliases, "doi")
        if doi is None:
            return None
        params = {"mailto": self._contact} if self._contact else None
        body = self._http.get_json(f"/works/{quote(doi, safe='/')}", params, allow_missing=True)
        if body is None:
            return None
        message = body.get("message") if isinstance(body, Mapping) else None
        if not isinstance(message, Mapping):
            raise SchemaChanged("Crossref 单篇查询缺少 message")
        basis = _license_basis(message)
        for url in _pdf_links(message)[: self._options.max_links]:
            content = self._download.fetch(url)
            if content is not None:
                return FullText(content, basis, self.name)
        return None


def _pdf_links(message: Mapping[str, Any]) -> list[str]:
    urls: list[str] = []
    for link in message.get("link") or []:
        if not isinstance(link, Mapping):
            continue
        url = link.get("URL")
        is_pdf = "pdf" in str(link.get("content-type", "")).lower()
        usable = str(link.get("intended-application", "unspecified")) in _APPLICATIONS
        if isinstance(url, str) and is_pdf and usable and url not in urls:
            urls.append(url)
    return urls


def _license_basis(message: Mapping[str, Any]) -> LicenseBasis:
    licenses = [
        str(item.get("URL", "")).lower()
        for item in message.get("license") or []
        if isinstance(item, Mapping)
    ]
    if licenses and all(any(m in url for m in _OPEN_LICENSE_MARKERS) for url in licenses):
        return "open"
    return "subscribed"
