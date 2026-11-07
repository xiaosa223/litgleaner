"""Zotero 10 本地 API（127.0.0.1:23119/api/）的请求细节：授权、Server-ID、错误分类。

规则以 Zotero 10.0.7 自带的 server_localAPI.js 和 server.js 为准（2026-11 核对）：
- 写请求必须带 Zotero-Server-ID（任何响应头里都有），缺了返回 428，不匹配返回 412。
- 写请求必须带本地 key：POST /api/local/authorize 后用户在 Zotero 弹窗里点“始终允许”拿到。
  它和 zotero.org 的 API key 无关；点“允许”拿到的是一次性 key，用一次就失效。授权本身也算写请求。
- Host 只能是 localhost 或 127.0.0.1；User-Agent 以 “Mozilla/” 开头会被当成浏览器拒绝。
- Zotero-Write-Token 只存在 Zotero 进程内存里（12 小时），重启就丢。
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from litfactory.domain.errors import (
    AuthFailed,
    ConfigError,
    Deferred,
    SchemaChanged,
    TransientError,
)

DEFAULT_BASE_URL = "http://127.0.0.1:23119"
KEY_ENV = "ZOTERO_LOCAL_API_KEY"
KEY_FILE = Path("~/.config/litfactory/zotero-local-key")
APP_NAME = "论文工厂 litfactory"
USER_PREFIX = "/api/users/0"
NOT_RUNNING_RETRY_SECONDS = 900
AUTHORIZE_TIMEOUT_SECONDS = 300
AUTHORIZE_HINT = "运行 litfactory zotero authorize，在 Zotero 弹窗里点“始终允许”"
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MESSAGE_LIMIT = 200


def default_key_file() -> Path:
    return KEY_FILE.expanduser()


def read_local_key(environ: Mapping[str, str], key_file: Path) -> str | None:
    value = environ.get(KEY_ENV, "").strip()
    if value:
        return value
    try:
        return key_file.read_text(encoding="utf-8").strip() or None
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConfigError(f"读取 Zotero 本地 key 文件 {key_file} 失败：{error}") from error


def save_local_key(key: str, key_file: Path) -> None:
    """只有本人可读写（0600）。先建文件再写内容，内容不会短暂地对其他用户可见。"""
    key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_file.touch(mode=0o600, exist_ok=True)
    key_file.chmod(0o600)
    key_file.write_text(key + "\n", encoding="utf-8")


class TokenUsed(TransientError):
    """同一个 Zotero-Write-Token 已经写成功过：上一次尝试其实写进去了。"""


class ServerChanged(TransientError):
    """Server-ID 对不上：Zotero 换了数据目录。缓存的分组 key 等都要作废。"""


class LocalApi:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if httpx.URL(base_url).host not in _LOCAL_HOSTS:
            raise ConfigError(f"Zotero 本地 API 只能连本机，base_url 不能是 {base_url}")
        self._api_key = api_key
        self._server_id: str | None = None
        # 本机请求不能走代理：trust_env=False 忽略 HTTP(S)_PROXY；本地 API 不该跳转，跳转视为异常。
        self._client = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": "litfactory (zotero local writer)"},
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def close(self) -> None:
        self._client.close()

    def server_id(self, *, refresh: bool = False) -> str:
        if self._server_id is None or refresh:
            response = self.send("GET", "/api/", auth=False, server_id=False)
            value = response.headers.get("Zotero-Server-ID", "").strip()
            if not value:
                raise SchemaChanged("Zotero 本地 API 响应里没有 Zotero-Server-ID")
            self._server_id = value
        return self._server_id

    def authorize(self) -> str:
        """弹出 Zotero 授权窗口，等用户点选。返回 key；用户拒绝或只允许一次时抛 AuthFailed。"""
        response = self.send(
            "POST",
            "/api/local/authorize",
            json={"appName": APP_NAME},
            auth=False,
            timeout=AUTHORIZE_TIMEOUT_SECONDS,
        )
        body = parse_json(response)
        key = body.get("key") if isinstance(body, Mapping) else None
        if not isinstance(key, str) or not key:
            raise SchemaChanged("Zotero 授权响应里没有 key")
        if not body.get("remember"):
            raise AuthFailed("选的是“允许”（一次性），请重新授权并选“始终允许”")
        return key

    def objects(
        self, path: str, params: Mapping[str, str] | None = None
    ) -> list[Mapping[str, Any]]:
        return parse_objects(parse_json(self.send("GET", path, params=params, auth=False)))

    def post_one(self, suffix: str, body: Mapping[str, Any], token: str) -> tuple[str, int] | None:
        """写入一个对象，返回（key，版本号）。同一 token 已经写成功过时返回 None。"""
        headers = {"Zotero-Write-Token": token}
        try:
            response = self.send("POST", f"{USER_PREFIX}{suffix}", json=[body], headers=headers)
        except TokenUsed:
            return None
        result = parse_json(response)
        if not isinstance(result, Mapping):
            raise SchemaChanged("Zotero 写入响应不是对象")
        failed = result.get("failed") or {}
        if failed:
            first = next(iter(failed.values()))
            message = first.get("message") if isinstance(first, Mapping) else first
            raise SchemaChanged(f"Zotero 拒绝写入：{str(message)[:_MESSAGE_LIMIT]}")
        value = (result.get("successful") or {}).get("0")
        if isinstance(value, Mapping) and isinstance(value.get("key"), str):
            version = value.get("version")
            return value["key"], version if isinstance(version, int) else 0
        raise SchemaChanged("Zotero 写入响应里没有新对象的 key")

    def send(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        server_id: bool = True,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        merged = dict(headers or {})
        if server_id:
            merged["Zotero-Server-ID"] = self.server_id()
        if auth:
            merged["Zotero-API-Key"] = self._api_key or ""
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = self._client.request(method, path, headers=merged, **kwargs)
        except httpx.ConnectError as error:
            raise Deferred(
                "连不上 Zotero：请打开 Zotero 桌面端（设置 → 高级 → 允许其他应用通信）",
                NOT_RUNNING_RETRY_SECONDS,
            ) from error
        except httpx.HTTPError as error:
            raise TransientError(f"请求 Zotero 失败：{type(error).__name__}") from error
        self._raise_for_status(response)
        return response

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status < 300:
            return
        detail = " ".join(response.text.split())[:_MESSAGE_LIMIT]
        if self._api_key:
            detail = detail.replace(self._api_key, "[secret]")
        text = f"Zotero 本地 API 返回 HTTP {status}：{detail}"
        if status == 401:
            raise AuthFailed(f"{text}。本地 key 无效或已被撤销：{AUTHORIZE_HINT}")
        if status == 403:
            if "not enabled" in detail:
                raise AuthFailed(f"{text}。请在 Zotero 设置 → 高级里开启“允许其他应用通信”")
            raise AuthFailed(f"{text}。在 Zotero 里拒绝了授权，或这个文库不允许写入")
        if status == 412 and "Write token already used" in detail:
            raise TokenUsed(text)
        if status == 412 and "Server-ID" in detail:
            self._server_id = None
            raise ServerChanged(text)
        if status == 429:
            raise Deferred(text, 60)
        if status >= 500:
            raise TransientError(text)
        raise SchemaChanged(f"{text}（请求不被接受，适配器可能需要修改）")


def parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise SchemaChanged("Zotero 本地 API 返回的不是 JSON") from error


def parse_objects(body: Any) -> list[Mapping[str, Any]]:
    if not isinstance(body, list):
        raise SchemaChanged("Zotero 本地 API 返回的不是列表")
    for entry in body:
        if not (
            isinstance(entry, Mapping)
            and isinstance(entry.get("key"), str)
            and isinstance(entry.get("data"), Mapping)
        ):
            raise SchemaChanged("Zotero 条目缺少 key 或 data")
    return body
