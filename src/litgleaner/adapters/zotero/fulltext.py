"""Zotero 文献库里已有的 PDF。文件由用户自己保存，license_basis 记为 user_supplied。

local：按 DOI（大小写不敏感，也看 extra）或 arXiv 编号找到条目，读它的 PDF 子附件。
web：Web API 的检索不覆盖 DOI 字段，先按标题检索，再在本地核对 DOI / arXiv 编号，
然后下载 PDF 附件。下载走 PdfDownloader：Zotero-API-Key 只发给 api.zotero.org，
不会随重定向转发到文件存储服务。找不到就返回 None，让下一个全文来源接着试。
"""

from collections.abc import Mapping
from typing import Any

import httpx

from litgleaner.adapters.common.download import PdfDownloader, Resolver, system_resolver
from litgleaner.adapters.common.options import parse_options
from litgleaner.adapters.common.records import alias_value
from litgleaner.adapters.zotero.discovery import discover_data_dir
from litgleaner.adapters.zotero.files import read_attachment
from litgleaner.adapters.zotero.library import LocalLibrary
from litgleaner.adapters.zotero.mapping import is_regular, item_arxiv, item_doi
from litgleaner.adapters.zotero.options import ZoteroFullTextOptions, require_api_key
from litgleaner.adapters.zotero.web import ZoteroWebApi
from litgleaner.domain.identity import normalize_arxiv_id, normalize_doi
from litgleaner.ports.fulltext import FullText, FullTextRequest

_STORED_MODES = frozenset({"imported_file", "imported_url"})


class ZoteroFullText:
    name = "zotero"

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        api_key: str | None = None,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._options = parse_options(ZoteroFullTextOptions, options, self.name)
        self._max_bytes = self._options.max_pdf_mb * 1_000_000
        self._web: ZoteroWebApi | None = None
        self._local: LocalLibrary | None = None
        key = require_api_key(self._options, api_key, self.name)
        if key is None:
            self._local = LocalLibrary(discover_data_dir(self._options.data_dir))
            return
        self._web = ZoteroWebApi(
            self._options,
            keep_raw=False,
            api_key=key,
            contact_email=contact_email,
            min_interval=self._options.min_interval_seconds,
            timeout=self._options.timeout_seconds,
            transport=transport,
        )
        self._download = PdfDownloader(
            allow_fake_ip=self._options.allow_proxy_fake_ip,
            max_bytes=self._max_bytes,
            timeout=self._options.timeout_seconds,
            transport=transport,
            resolver=resolver,
        )

    def fetch(self, request: FullTextRequest) -> FullText | None:
        doi = normalize_doi(alias_value(request.aliases, "doi"))
        arxiv = normalize_arxiv_id(alias_value(request.aliases, "arxiv"))
        if doi is None and arxiv is None:
            return None
        if self._local is not None:
            content = self._local_pdf(self._local, doi, arxiv)
        elif self._web is not None:
            content = self._web_pdf(self._web, request.title, doi, arxiv)
        else:
            content = None
        return FullText(content, "user_supplied", self.name) if content else None

    def _local_pdf(self, library: LocalLibrary, doi: str | None, arxiv: str | None) -> bytes | None:
        attachments = library.pdf_attachments(doi=doi, arxiv=arxiv)
        if not attachments and doi and arxiv:
            attachments = library.pdf_attachments(doi=None, arxiv=arxiv)
        for attachment in attachments:
            content = read_attachment(library.data_dir, attachment, self._max_bytes)
            if content is not None:
                return content
        return None

    def _web_pdf(
        self, api: ZoteroWebApi, title: str, doi: str | None, arxiv: str | None
    ) -> bytes | None:
        if not title.strip():
            return None
        for item in api.search(title.strip(), self._options.max_candidates):
            data = item["data"]
            if not is_regular(data) or not _same_paper(data, doi, arxiv):
                continue
            for child in api.children(str(item["key"])):
                content = self._web_attachment(api, child)
                if content is not None:
                    return content
        return None

    def _web_attachment(self, api: ZoteroWebApi, child: Mapping[str, Any]) -> bytes | None:
        data = child["data"]
        is_pdf = str(data.get("contentType", "")).lower() == "application/pdf"
        stored = data.get("linkMode") in _STORED_MODES
        if data.get("itemType") != "attachment" or not is_pdf or not stored or data.get("deleted"):
            return None
        return self._download.fetch(api.file_url(str(child["key"])), headers=api.auth_headers)


def _same_paper(data: Mapping[str, Any], doi: str | None, arxiv: str | None) -> bool:
    return bool((doi and item_doi(data) == doi) or (arxiv and item_arxiv(data) == arxiv))
