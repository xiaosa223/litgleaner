"""Semantic Scholar 登记的开放获取 PDF（openAccessPdf）。

按 DOI 查，没有 DOI 时按 arXiv 编号查，取 openAccessPdf.url 交给 PdfDownloader 下载。
Semantic Scholar 只登记它认为可公开获取的链接，所以记为 open；链接打不开或不是 PDF 就返回 None。
没有 key 也能用，但共享限流池经常 429，默认请求间隔放大到 3 秒。
文档：https://api.semanticscholar.org/api-docs/graph#tag/Paper-Data/operation/get_graph_get_paper
"""

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from litgleaner.adapters.common.download import PdfDownloader, Resolver, system_resolver
from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.common.options import FullTextOptions, parse_options
from litgleaner.adapters.common.records import alias_value
from litgleaner.domain.errors import SchemaChanged
from litgleaner.ports.fulltext import FullText, FullTextRequest

_INTERVAL_WITH_KEY = 1.0
_INTERVAL_WITHOUT_KEY = 3.0


class SemanticScholarFullTextOptions(FullTextOptions):
    base_url: str = "https://api.semanticscholar.org/graph/v1"


class SemanticScholarFullText:
    name = "semantic_scholar"

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._options = parse_options(SemanticScholarFullTextOptions, options, self.name)
        default_interval = _INTERVAL_WITH_KEY if api_key else _INTERVAL_WITHOUT_KEY
        interval = self._options.min_interval_seconds
        self._http = HttpClient(
            "Semantic Scholar",
            keep_raw=False,
            base_url=self._options.base_url,
            contact_email=contact_email,
            secrets=[api_key],
            headers={"x-api-key": api_key} if api_key else None,
            min_interval=interval if "min_interval_seconds" in options else default_interval,
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
        """先按 DOI 查；Semantic Scholar 没收录这个 DOI 时再按 arXiv 编号查。"""
        for paper_id in _paper_ids(request):
            body = self._http.get_json(
                f"/paper/{quote(paper_id, safe='/:')}",
                {"fields": "openAccessPdf"},
                allow_missing=True,
            )
            if body is None:
                continue
            if not isinstance(body, Mapping):
                raise SchemaChanged("Semantic Scholar 单篇查询返回的不是对象")
            url = _pdf_url(body)
            content = self._download.fetch(url) if url else None
            return None if content is None else FullText(content, "open", self.name)
        return None


def _paper_ids(request: FullTextRequest) -> list[str]:
    doi = alias_value(request.aliases, "doi")
    arxiv = alias_value(request.aliases, "arxiv")
    return [*([f"DOI:{doi}"] if doi else []), *([f"ARXIV:{arxiv}"] if arxiv else [])]


def _pdf_url(body: Mapping[str, Any]) -> str | None:
    record = body.get("openAccessPdf")
    url = record.get("url") if isinstance(record, Mapping) else None
    return url.strip() if isinstance(url, str) and url.strip() else None
