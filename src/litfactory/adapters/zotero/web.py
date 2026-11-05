"""Zotero Web API v3 的读取部分。文档：https://www.zotero.org/support/dev/web_api/v3/basics

key 只放在 Zotero-API-Key 请求头里，不放进 URL，原始留档里看不到它。
实测（2026-11）：quick search 的 q 不检索 DOI 字段（qmode=everything 也不行），
所以全文来源按标题检索后再在本地核对 DOI / arXiv 编号。
"""

from collections.abc import Mapping
from typing import Any

import httpx

from litfactory.adapters.common.http import HttpClient
from litfactory.adapters.zotero.options import LibraryOptions
from litfactory.domain.errors import ConfigError, SchemaChanged

API_VERSION = "3"
MAX_KEYS_PER_REQUEST = 50
MAX_PAGE = 100

Item = Mapping[str, Any]


class ZoteroWebApi:
    def __init__(
        self,
        options: LibraryOptions,
        *,
        api_key: str,
        contact_email: str | None,
        min_interval: float,
        timeout: float,
        transport: httpx.BaseTransport | None,
        keep_raw: bool = True,
    ) -> None:
        self.library_path = options.library_path
        self.base_url = options.base_url.rstrip("/")
        self.auth_headers = {"Zotero-API-Key": api_key, "Zotero-API-Version": API_VERSION}
        self._http = HttpClient(
            "Zotero",
            keep_raw=keep_raw,
            base_url=self.base_url,
            contact_email=contact_email,
            secrets=(api_key,),
            headers=self.auth_headers,
            min_interval=min_interval,
            timeout=timeout,
            transport=transport,
        )

    def reset_raw_log(self) -> None:
        self._http.reset_raw_log()

    def raw_log(self) -> bytes:
        return self._http.raw_log()

    def close(self) -> None:
        self._http.close()

    def _path(self, suffix: str, collection: str | None = None) -> str:
        scope = f"/collections/{collection}" if collection else ""
        return f"/{self.library_path}{scope}{suffix}"

    def top_items(
        self, collection: str | None, *, start: int, limit: int
    ) -> tuple[list[Item], str]:
        """顶层条目，按加入时间从新到旧。返回本页条目和库版本号。"""
        params = {
            "sort": "dateAdded",
            "direction": "desc",
            "itemType": "-attachment",
            "format": "json",
            "start": str(start),
            "limit": str(min(limit, MAX_PAGE)),
        }
        response = self._get(self._path("/items/top", collection), params)
        return _items(response), _library_version(response)

    def versions(self, collection: str | None, since: int) -> tuple[dict[str, int], str]:
        """自 since 版本以来新增或修改的顶层条目：key → 条目版本号。"""
        params = {"since": str(since), "format": "versions", "itemType": "-attachment"}
        response = self._get(self._path("/items/top", collection), params)
        body = _json(response)
        if not isinstance(body, Mapping) or not all(
            isinstance(key, str) and isinstance(value, int) for key, value in body.items()
        ):
            raise SchemaChanged("Zotero format=versions 返回的不是 key → 版本号")
        return dict(body), _library_version(response)

    def items_by_keys(self, keys: list[str]) -> list[Item]:
        wanted = set(keys)
        found: dict[str, Item] = {}
        for start in range(0, len(keys), MAX_KEYS_PER_REQUEST):
            chunk = keys[start : start + MAX_KEYS_PER_REQUEST]
            params = {"itemKey": ",".join(chunk), "format": "json", "limit": str(MAX_PAGE)}
            for item in _items(self._get(self._path("/items"), params)):
                if item["key"] in wanted:
                    found.setdefault(item["key"], item)
        return [found[key] for key in keys if key in found]

    def collection_key(self, value: str) -> str:
        """collection 选项可以写名称或 key，统一成 key。"""
        start = 0
        while True:
            params = {"format": "json", "start": str(start), "limit": str(MAX_PAGE)}
            page = _items(self._get(self._path("/collections"), params))
            for entry in page:
                if entry["key"] == value or entry["data"].get("name") == value:
                    return str(entry["key"])
            if len(page) < MAX_PAGE:
                raise ConfigError(
                    f"Zotero 文献库里找不到 collection “{value}”（按名称或 key 查找）"
                )
            start += len(page)

    def search(self, title: str, limit: int) -> list[Item]:
        params = {"q": title, "itemType": "-attachment", "format": "json", "limit": str(limit)}
        return _items(self._get(self._path("/items"), params))

    def children(self, key: str) -> list[Item]:
        return _items(self._get(self._path(f"/items/{key}/children"), {"format": "json"}))

    def file_url(self, key: str) -> str:
        return f"{self.base_url}{self._path(f'/items/{key}/file')}"

    def _get(self, path: str, params: Mapping[str, str]) -> httpx.Response:
        response = self._http.get(path, params)
        if response is None:  # 没有 allow_missing 时 HttpClient 不会返回 None
            raise SchemaChanged(f"Zotero {path} 没有返回内容")
        return response


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise SchemaChanged("Zotero 返回的不是 JSON") from error


def _items(response: httpx.Response) -> list[Item]:
    body = _json(response)
    if not isinstance(body, list):
        raise SchemaChanged("Zotero 返回的不是条目列表")
    for entry in body:
        valid = isinstance(entry, Mapping) and isinstance(entry.get("key"), str)
        if not valid or not isinstance(entry.get("data"), Mapping):
            raise SchemaChanged("Zotero 条目缺少 key 或 data")
    return body


def _library_version(response: httpx.Response) -> str:
    version = str(response.headers.get("Last-Modified-Version", "")).strip()
    if not _is_number(version):
        raise SchemaChanged("Zotero 响应缺少 Last-Modified-Version")
    return version


def _is_number(text: str) -> bool:
    """只认 ASCII 数字。str.isdigit() 对“²”之类也返回 True，随后 int() 会抛错。"""
    return text.isascii() and text.isdecimal()
