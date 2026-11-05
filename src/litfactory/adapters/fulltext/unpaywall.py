"""Unpaywall 登记的开放获取 PDF。

按 DOI 查询，取 best_oa_location 的 url_for_pdf，再依次试其他 oa_locations。只下载开放获取链接，
记为 open。Unpaywall 要求每次请求带上真实联系邮箱（占位邮箱会被 422 拒绝），所以邮箱是必需配置。
没有 DOI、DOI 不存在、没有 PDF 链接时返回 None。
文档：https://unpaywall.org/products/api
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import Field

from litfactory.adapters.common.download import PdfDownloader, Resolver, system_resolver
from litfactory.adapters.common.http import HttpClient
from litfactory.adapters.common.options import FullTextOptions, parse_options
from litfactory.adapters.common.records import alias_value
from litfactory.domain.errors import ConfigError, SchemaChanged
from litfactory.ports.fulltext import FullText, FullTextRequest


class UnpaywallOptions(FullTextOptions):
    base_url: str = "https://api.unpaywall.org"
    max_links: int = Field(default=3, gt=0, le=10)
    min_interval_seconds: float = Field(default=0.2, ge=0)


class UnpaywallFullText:
    name = "unpaywall"

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._options = parse_options(UnpaywallOptions, options, self.name)
        if not contact_email or not contact_email.strip():
            raise ConfigError(
                "unpaywall 需要真实的联系邮箱：请设置 LITFACTORY_CONTACT_EMAIL"
                "（Unpaywall 拒绝匿名请求）"
            )
        self._email = contact_email.strip()
        self._http = HttpClient(
            "Unpaywall",
            keep_raw=False,
            base_url=self._options.base_url,
            contact_email=self._email,
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
        body = self._http.get_json(
            f"/v2/{quote(doi, safe='/')}", {"email": self._email}, allow_missing=True
        )
        if body is None:
            return None
        if not isinstance(body, Mapping):
            raise SchemaChanged("Unpaywall 返回的不是对象")
        for url in _pdf_links(body)[: self._options.max_links]:
            content = self._download.fetch(url)
            if content is not None:
                return FullText(content, "open", self.name)
        return None


def _pdf_links(record: Mapping[str, Any]) -> list[str]:
    candidates: list[Any] = [record.get("best_oa_location")]
    locations = record.get("oa_locations")
    if isinstance(locations, list):
        candidates.extend(locations)
    urls: list[str] = []
    for location in candidates:
        url = location.get("url_for_pdf") if isinstance(location, Mapping) else None
        if isinstance(url, str) and url.startswith(("http://", "https://")) and url not in urls:
            urls.append(url)
    return urls
