"""按外部 API 给出的链接下载 PDF。

链接来自第三方数据，不可信：只允许 http(s)，每一跳重定向都检查目标不是内网或本机，
限制大小，并且只接受以 %PDF- 开头的内容。拿不到就返回 None，让下一个全文来源接着试。
已知残留风险：检查和连接之间的 DNS 重绑定无法在这一层完全排除。

代理的 fake-IP 模式（Clash、Surge、Shadowrocket 等）会把所有域名解析到 198.18.0.0/15，
真实地址由代理决定，本地无法检查。默认放行这种解析结果，否则这些用户完全无法下载；
URL 里直接写的 IP 不享受这个例外。对安全要求更高的部署可以关闭 allow_fake_ip。
"""

import ipaddress
import logging
import socket
import time
from collections.abc import Callable, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from litfactory.adapters.common.http import retry_after_seconds, user_agent
from litfactory.domain.errors import RateLimited

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
DEFAULT_MAX_BYTES = 80_000_000
# httpx 的超时只限制单次读写的间隔；慢速服务器一点点地发数据可以无限拖住下载，所以另设总时限。
DEFAULT_DEADLINE_SECONDS = 180.0
MAX_REDIRECTS = 5
_REDIRECTS = frozenset({301, 302, 303, 307, 308})

Resolver = Callable[[str], list[str]]
FAKE_IP_RANGE = ipaddress.ip_network("198.18.0.0/15")
# Python 认为是 global、但可能转到内网的地址段：NAT64 前缀、IETF 协议地址、废弃的站点本地地址。
_EXTRA_BLOCKED = tuple(
    ipaddress.ip_network(net)
    for net in ("64:ff9b::/96", "64:ff9b:1::/48", "192.0.0.0/24", "fec0::/10")
)
_INTERNAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".lan",
    ".internal",
    ".localdomain",
    ".home.arpa",
    ".intranet",
    ".corp",
)


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


def _ip(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return None


def _is_public(address: str, allow_fake_ip: bool) -> bool:
    ip = _ip(address)
    if ip is None:
        return False
    if allow_fake_ip and ip in FAKE_IP_RANGE:
        return True
    if any(ip in net for net in _EXTRA_BLOCKED if net.version == ip.version):
        return False
    return ip.is_global and not ip.is_multicast


def _internal_name(host: str) -> bool:
    """内网常用的主机名。fake-IP 代理下它们也会被解析到 198.18.x.x，必须按名字拒绝。"""
    name = host.rstrip(".").lower()
    return name == "localhost" or "." not in name or name.endswith(_INTERNAL_SUFFIXES)


class PdfDownloader:
    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = system_resolver,
        allow_fake_ip: bool = True,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_bytes = max_bytes
        self._deadline_seconds = deadline_seconds
        self._monotonic = monotonic
        self._resolver = resolver
        self._allow_fake_ip = allow_fake_ip
        self._client = httpx.Client(
            # 链接指向任意第三方主机，不带联系邮箱，避免把邮箱泄露给它们。
            headers={"User-Agent": user_agent(None), "Accept": "application/pdf,*/*"},
            timeout=timeout,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def fetch(self, url: str, headers: Mapping[str, str] | None = None) -> bytes | None:
        """headers 只发给最初的地址，不随重定向转发到其他主机。畸形链接视为没有全文。"""
        try:
            return self._fetch(url, headers)
        except ValueError as error:
            logger.info("链接无效，已跳过：%s", type(error).__name__)
            return None

    def _fetch(self, url: str, headers: Mapping[str, str] | None) -> bytes | None:
        current, extra = url, dict(headers or {})
        deadline = self._monotonic() + self._deadline_seconds
        self._client.cookies.clear()
        for _ in range(MAX_REDIRECTS + 1):
            if not self._allowed(current) or self._monotonic() > deadline:
                return None
            try:
                with self._client.stream("GET", current, headers=extra) as response:
                    location = response.headers.get("Location")
                    if response.status_code in _REDIRECTS and location:
                        target = urljoin(current, location)
                        if _downgrades(current, target):
                            logger.info("拒绝从 https 重定向到 http：%s", _host(target))
                            return None
                        current, extra = target, {}
                        continue
                    return self._read(current, response, deadline)
            except httpx.HTTPError as error:
                logger.info("下载失败 %s：%s", _host(current), type(error).__name__)
                return None
        logger.info("重定向次数过多：%s", _host(url))
        return None

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return False
        if _ip(parts.hostname) is None and _internal_name(parts.hostname):
            logger.warning("拒绝下载内网主机名：%s", parts.hostname)
            return False
        literal = _ip(parts.hostname) is not None
        addresses = [parts.hostname] if literal else self._resolver(parts.hostname)
        allow_fake = self._allow_fake_ip and not literal
        if not addresses or not all(_is_public(address, allow_fake) for address in addresses):
            logger.warning("拒绝下载非公网地址：%s", parts.hostname)
            return False
        return True

    def _read(self, url: str, response: httpx.Response, deadline: float) -> bytes | None:
        status = response.status_code
        if status == 429:
            wait = retry_after_seconds(response.headers.get("Retry-After"))
            raise RateLimited(f"{_host(url)} 限制下载频率", retry_after_seconds=wait)
        if status != 200:
            logger.info("%s 返回 HTTP %s，视为没有全文", _host(url), status)
            return None
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            if not chunks and not chunk.startswith(PDF_MAGIC[: len(chunk)]):
                return None
            total += len(chunk)
            if self._monotonic() > deadline:
                logger.warning("%s 下载超过 %s 秒，已放弃", _host(url), self._deadline_seconds)
                return None
            if total > self._max_bytes:
                logger.warning("%s 的文件超过 %s 字节，已放弃", _host(url), self._max_bytes)
                return None
            chunks.append(chunk)
        content = b"".join(chunks)
        return content if content.startswith(PDF_MAGIC) else None


def _downgrades(source: str, target: str) -> bool:
    return urlsplit(source).scheme == "https" and urlsplit(target).scheme != "https"


def _host(url: str) -> str:
    return urlsplit(url).hostname or "?"
