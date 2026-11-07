"""学术 API 共用的 HTTP 客户端：限速、错误分类、密钥脱敏、原始响应留档。

重试和退避由任务调度负责：这里只把失败归类成统一的错误类型，不在请求内反复重试。
"""

import json
import time
from collections.abc import Callable, Iterable, Mapping
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

from litfactory.domain.errors import (
    AuthFailed,
    RateLimited,
    SchemaChanged,
    TransientError,
)

DEFAULT_RETRY_SECONDS = 900
_MESSAGE_LIMIT = 200
_MISSING = frozenset({404, 410})


def _client_version() -> str:
    try:
        return version("litfactory")
    except PackageNotFoundError:
        return "dev"


def user_agent(contact_email: str | None) -> str:
    """多数学术 API 要求客户端可识别，带上联系邮箱会进入更稳定的“礼貌通道”。"""
    contact = f"; mailto:{contact_email}" if contact_email else ""
    return f"litfactory/{_client_version()} (literature monitor{contact})"


def retry_after_seconds(value: str | None, now: float | None = None) -> int:
    if not value:
        return DEFAULT_RETRY_SECONDS
    text = value.strip()
    if text.isascii() and text.isdecimal():
        return max(1, int(text))
    try:
        moment = parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return DEFAULT_RETRY_SECONDS
    return max(1, int(moment - (now if now is not None else time.time())))


class HttpClient:
    def __init__(
        self,
        service: str,
        *,
        base_url: str = "",
        contact_email: str | None = None,
        secrets: Iterable[str | None] = (),
        headers: Mapping[str, str] | None = None,
        min_interval: float = 0.0,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        keep_raw: bool = True,
    ) -> None:
        self.service = service
        # 全文来源不需要原始留档，关掉以免长时间运行时内存一直增长。
        self._keep_raw = keep_raw
        plain = tuple(s for s in secrets if s)
        # URL 里的密钥是编码后的形式，也要一起脱敏。
        self._secrets = tuple(
            dict.fromkeys(v for s in plain for v in (s, quote(s, safe=""), quote_plus(s)))
        )
        self._origin = httpx.URL(base_url).host if base_url else None
        self._secret_headers = frozenset(
            name.lower()
            for name, value in (headers or {}).items()
            if any(secret in value for secret in plain)
        )
        self._client = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": user_agent(contact_email), **(headers or {})},
            timeout=timeout,
            follow_redirects=True,
            transport=transport,
            event_hooks={"request": [self._drop_secrets_off_origin]},
        )
        self._min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: float | None = None
        self._pages: list[bytes] = []

    def _drop_secrets_off_origin(self, request: httpx.Request) -> None:
        """重定向到其他主机时去掉带密钥的请求头（httpx 只会自动去掉 Authorization）。"""
        if self._origin is None or request.url.host == self._origin:
            return
        for name in self._secret_headers:
            request.headers.pop(name, None)

    def scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[secret]")
        return text

    def _wait_turn(self) -> None:
        if self._last is not None and self._min_interval > 0:
            remaining = self._min_interval - (self._monotonic() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._monotonic()

    def get(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        allow_missing: bool = False,
    ) -> httpx.Response | None:
        """返回成功的响应；allow_missing 时 404/410 返回 None。其余失败抛统一错误。"""
        self._wait_turn()
        try:
            response = self._client.get(path, params=params, headers=headers)
        except httpx.HTTPError as error:
            raise TransientError(f"连接 {self.service} 失败：{type(error).__name__}") from error
        self._remember(response)
        if allow_missing and response.status_code in _MISSING:
            return None
        self._raise_for_status(response)
        return response

    def get_json(
        self,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        allow_missing: bool = False,
    ) -> Any:
        response = self.get(path, params, headers=headers, allow_missing=allow_missing)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError as error:
            raise SchemaChanged(f"{self.service} 返回的不是 JSON") from error

    def post_json(
        self, path: str, body: Mapping[str, Any], *, headers: Mapping[str, str] | None = None
    ) -> Any:
        """POST JSON 并解析 JSON 响应。错误分类与 get 相同。"""
        self._wait_turn()
        try:
            response = self._client.post(path, json=body, headers=headers)
        except httpx.HTTPError as error:
            raise TransientError(f"连接 {self.service} 失败：{type(error).__name__}") from error
        self._remember(response)
        self._raise_for_status(response)
        try:
            return response.json()
        except ValueError as error:
            raise SchemaChanged(f"{self.service} 返回的不是 JSON") from error

    def get_text(
        self, path: str, params: Mapping[str, Any] | None = None, *, allow_missing: bool = False
    ) -> str | None:
        response = self.get(path, params, allow_missing=allow_missing)
        return None if response is None else response.text

    def raw_log(self) -> bytes:
        """本次调用收到的所有页面，每行一个 JSON。发现阶段会原样落盘，便于重放。"""
        return b"\n".join(self._pages)

    def reset_raw_log(self) -> None:
        self._pages = []

    def close(self) -> None:
        self._client.close()

    def _remember(self, response: httpx.Response) -> None:
        if not self._keep_raw:
            return
        entry = {
            "url": self.scrub(str(response.request.url)),
            "status": response.status_code,
            "body": self.scrub(response.text),
        }
        self._pages.append(json.dumps(entry, ensure_ascii=False).encode("utf-8"))

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 400:
            return
        detail = self.scrub(response.text.strip().replace("\n", " "))[:_MESSAGE_LIMIT]
        text = f"{self.service} 返回 HTTP {status}：{detail}"
        if status in {401, 403}:
            raise AuthFailed(f"{text}。请检查 API key 或访问权限")
        if status == 429:
            header = response.headers.get("Retry-After") or response.headers.get(
                "X-RateLimit-Retry-After"
            )
            raise RateLimited(text, retry_after_seconds=retry_after_seconds(header))
        if status in {400, 422}:
            raise SchemaChanged(f"{text}（请求不被接受，适配器可能需要修改）")
        raise TransientError(text)
