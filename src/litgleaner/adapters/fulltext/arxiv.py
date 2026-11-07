"""arXiv 预印本全文：论文有 arXiv 编号时直接下载 PDF。

编号来自论文的 arxiv 别名；只有 DOI 时，arXiv 自己注册的 DataCite DOI（10.48550/arXiv.编号）
也能还原出编号。arXiv 上的预印本都可以公开获取，记为 open。
arXiv 要求自动下载放慢速度，同一个实例内两次下载至少间隔 min_interval_seconds（默认 3 秒）。
"""

import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from pydantic import Field

from litgleaner.adapters.common.download import PdfDownloader, Resolver, system_resolver
from litgleaner.adapters.common.options import FullTextOptions, parse_options
from litgleaner.adapters.common.records import alias_value
from litgleaner.domain.identity import Alias, normalize_arxiv_id
from litgleaner.ports.fulltext import FullText, FullTextRequest

_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.IGNORECASE)


class ArxivFullTextOptions(FullTextOptions):
    base_url: str = "https://arxiv.org"
    min_interval_seconds: float = Field(default=3.0, ge=0)
    timeout_seconds: float = Field(default=90, gt=0)


class ArxivFullText:
    name = "arxiv"

    def __init__(
        self,
        options: Mapping[str, Any],
        *,
        contact_email: str | None,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._options = parse_options(ArxivFullTextOptions, options, self.name)
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None
        self._download = PdfDownloader(
            allow_fake_ip=self._options.allow_proxy_fake_ip,
            max_bytes=self._options.max_pdf_mb * 1_000_000,
            timeout=self._options.timeout_seconds,
            transport=transport,
            resolver=resolver,
        )

    def fetch(self, request: FullTextRequest) -> FullText | None:
        arxiv_id = arxiv_id_from_aliases(request.aliases)
        if arxiv_id is None:
            return None
        self._wait_turn()
        content = self._download.fetch(f"{self._options.base_url.rstrip('/')}/pdf/{arxiv_id}")
        return FullText(content, "open", self.name) if content is not None else None

    def _wait_turn(self) -> None:
        interval = self._options.min_interval_seconds
        if self._last is not None and interval > 0:
            remaining = interval - (self._monotonic() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._monotonic()


def arxiv_id_from_aliases(aliases: tuple[Alias, ...]) -> str | None:
    """优先用 arxiv 别名；没有时从 10.48550/arXiv.编号 形式的 DOI 里取。"""
    direct = normalize_arxiv_id(alias_value(aliases, "arxiv"))
    if direct:
        return direct
    match = _ARXIV_DOI.match(alias_value(aliases, "doi") or "")
    return normalize_arxiv_id(match.group(1)) if match else None
