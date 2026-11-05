"""Europe PMC / PMC 开放获取全文。

只取开放获取的文章，全部记为 open，PDF 一律经 PdfDownloader 下载：
1. 有 PMCID 时，先取 PMC 开放子集在云端的官方副本（NCBI PMC Cloud Service，公开的
   S3 存储桶 pmc-oa-opendata）。元数据里 is_pmc_openaccess 为真才下载，作者手稿等不取。
2. 没有云端副本，或者只有 DOI 时，在 Europe PMC 检索这篇文章（按 PMCID，没有时按 DOI）。
   记录须是开放获取：isOpenAccess=Y（PMC 开放子集），或者许可证是知识共享协议（常见于预印本）。
   依次尝试记录里标注为开放获取（availabilityCode=OA）的 PDF 链接：Europe PMC 自己的
   渲染地址在前，其次是出版商或预印本服务器的地址。
europepmc.org 网页前有人机验证，被拦时下载器拿到 403，自动换下一个链接；不尝试绕过。
文档：https://europepmc.org/RestfulWebService 、https://pmc.ncbi.nlm.nih.gov/tools/cloud/
"""

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from litfactory.adapters.common.download import PdfDownloader, Resolver, system_resolver
from litfactory.adapters.common.http import HttpClient
from litfactory.adapters.common.options import FullTextOptions, parse_options
from litfactory.adapters.common.records import alias_value
from litfactory.domain.errors import SchemaChanged, TransientError
from litfactory.ports.fulltext import FullText, FullTextRequest

DEFAULT_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest"
_RENDER = "https://europepmc.org/articles/{pmcid}?pdf=render"
_CLOUD_BUCKET = "pmc-oa-opendata"
_S3_KEY = "{http://s3.amazonaws.com/doc/2006-03-01/}Key"


class EuropePmcFullTextOptions(FullTextOptions):
    base_url: str = DEFAULT_BASE_URL
    pmc_cloud_url: str = f"https://{_CLOUD_BUCKET}.s3.amazonaws.com"
    use_pmc_cloud: bool = True
    max_links: int = Field(default=3, gt=0, le=10)
    min_interval_seconds: float = Field(default=0.5, ge=0)


class EuropePmcFullText:
    name = "europe_pmc"

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self._options = parse_options(EuropePmcFullTextOptions, options, self.name)
        self._contact = contact_email
        self._http = HttpClient(
            "Europe PMC",
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
        pmcid = alias_value(request.aliases, "pmcid")
        if pmcid and (found := self._from_cloud(pmcid.upper())):
            return found
        record = self._lookup(pmcid, alias_value(request.aliases, "doi"))
        if record is None or not _is_open(record):
            return None
        found_pmcid = record.get("pmcid")
        in_open_subset = isinstance(found_pmcid, str) and record.get("isOpenAccess") == "Y"
        if not pmcid and in_open_subset and (found := self._from_cloud(str(found_pmcid).upper())):
            return found
        return self._first_pdf(_pdf_links(record))

    def _first_pdf(self, urls: list[str]) -> FullText | None:
        for url in urls[: self._options.max_links]:
            content = self._download.fetch(url)
            if content is not None:
                return FullText(content, "open", self.name)
        return None

    def _lookup(self, pmcid: str | None, doi: str | None) -> Mapping[str, Any] | None:
        if pmcid:
            clause = f"PMCID:{_quoted(pmcid.upper())}"
        elif doi:
            clause = f"DOI:{_quoted(doi)}"
        else:
            return None
        params = {"query": clause, "format": "json", "resultType": "core", "pageSize": "1"}
        if self._contact:
            params["email"] = self._contact
        results = _search_results(self._http.get_json("/search", params))
        return results[0] if results else None

    def _from_cloud(self, pmcid: str) -> FullText | None:
        """PMC 开放子集的云端副本：先列出元数据文件找最新版本，再按元数据给出的地址下载。"""
        if not self._options.use_pmc_cloud or not re.fullmatch(r"PMC\d+", pmcid):
            return None
        root = self._options.pmc_cloud_url.rstrip("/")
        params = {"list-type": "2", "prefix": f"metadata/{pmcid}."}
        listing = self._http.get_text(f"{root}/", params, allow_missing=True)
        key = _latest_metadata_key(listing, pmcid)
        if key is None:
            return None
        meta = self._http.get_json(f"{root}/{key}", allow_missing=True)
        if not isinstance(meta, Mapping) or meta.get("is_pmc_openaccess") is not True:
            return None
        url = _cloud_url(root, meta.get("pdf_url"))
        return self._first_pdf([url]) if url else None


def _quoted(value: str) -> str:
    return '"' + value.replace('"', " ").strip() + '"'


def _search_results(body: Any) -> list[Mapping[str, Any]]:
    """服务端故障有时以 200 + errCode 返回。与信号源适配器同样的判断，各自保留一份。"""
    if isinstance(body, Mapping) and body.get("errCode"):
        message = f"Europe PMC 返回错误 {body.get('errCode')}：{str(body.get('errMsg'))[:200]}"
        if str(body.get("errCode")).startswith("5"):
            raise TransientError(message)
        raise SchemaChanged(message)
    result_list = body.get("resultList") if isinstance(body, Mapping) else None
    results = result_list.get("result") if isinstance(result_list, Mapping) else None
    if not isinstance(results, list):
        raise SchemaChanged("Europe PMC 返回缺少 resultList.result")
    return [item for item in results if isinstance(item, Mapping)]


def _latest_metadata_key(listing: str | None, pmcid: str) -> str | None:
    if not listing:
        return None
    try:
        keys = [node.text or "" for node in ET.fromstring(listing).iter(_S3_KEY)]
    except ET.ParseError:
        return None
    pattern = re.compile(rf"metadata/{pmcid}\.(\d+)\.json")
    versions = {int(m.group(1)): key for key in keys if (m := pattern.fullmatch(key))}
    return versions[max(versions)] if versions else None


def _cloud_url(root: str, value: Any) -> str | None:
    """s3://pmc-oa-opendata/PMC1.1/PMC1.1.pdf?md5=... 转成 HTTPS 地址。只认这个存储桶。"""
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    if parts.scheme != "s3" or parts.netloc != _CLOUD_BUCKET or not parts.path.endswith(".pdf"):
        return None
    return f"{root}{parts.path}"


def _is_open(record: Mapping[str, Any]) -> bool:
    license_name = str(record.get("license") or "").strip().lower()
    return record.get("isOpenAccess") == "Y" or license_name.startswith("cc")


def _pdf_links(record: Mapping[str, Any]) -> list[str]:
    url_list = record.get("fullTextUrlList")
    entries = url_list.get("fullTextUrl") if isinstance(url_list, Mapping) else None
    own: list[str] = []
    others: list[str] = []
    pmcid = record.get("pmcid")
    if isinstance(pmcid, str) and record.get("inEPMC") == "Y" and record.get("hasPDF") == "Y":
        own.append(_RENDER.format(pmcid=pmcid))
    for entry in entries or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("url"), str):
            continue
        if entry.get("documentStyle") != "pdf" or entry.get("availabilityCode") != "OA":
            continue
        (own if entry.get("site") == "Europe_PMC" else others).append(entry["url"])
    return list(dict.fromkeys([*own, *others]))
