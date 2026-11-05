"""MinerU 精准解析 API 的 HTTP 调用和错误分类。

依据官方文档（mineru.net/apiManage/docs）和 2026-11-02 的实测调用。
token 只发给 MinerU 的 API 地址；上传和下载用的是不带 token 的独立客户端。
"""

import re
from collections.abc import Mapping
from typing import Any

import httpx

from litfactory.domain.errors import (
    AuthFailed,
    PermanentError,
    RateLimited,
    SchemaChanged,
    TransientError,
)

_AUTH_CODES = frozenset({"A0202", "A0211"})
_BAD_FILE_CODES = frozenset({-60002, -60003, -60004, -60005, -60006, -60015, -60017})
_OUR_BUG_CODES = frozenset({-500, -10002})
_QUOTA_CODES = frozenset({-60018})
TASK_NOT_FOUND = -60012
QUOTA_RETRY_SECONDS = 6 * 3600
_URL = re.compile(r"https?://\S+")
_MESSAGE_LIMIT = 200


def scrub(text: str, secret: str) -> str:
    """错误信息里不能出现 token 和签名链接。"""
    cleaned = text.replace(secret, "[token]") if secret else text
    return _URL.sub("[url]", cleaned)[:_MESSAGE_LIMIT]


class TaskNotFound(TransientError):
    """续查的任务在服务端已不存在，需要重新提交。"""


def _raise_for_code(code: Any, message: str) -> None:
    text = f"MinerU 返回 {code}：{message}"
    if str(code) in _AUTH_CODES:
        raise AuthFailed(f"{text}。请到 https://mineru.net/apiManage/token 更换 token")
    if code in _BAD_FILE_CODES:
        raise PermanentError(text)
    if code in _OUR_BUG_CODES:
        raise SchemaChanged(f"{text}（请求参数不被接受，需要修改适配器）")
    if code in _QUOTA_CODES:
        raise RateLimited(text, retry_after_seconds=QUOTA_RETRY_SECONDS)
    if code == TASK_NOT_FOUND:
        raise TaskNotFound(text)
    raise TransientError(text)


def _raise_for_status(response: httpx.Response, what: str) -> None:
    status = response.status_code
    if status in {401, 403}:
        raise AuthFailed(f"{what} 被拒绝（HTTP {status}），token 可能无效或已过期")
    if status == 429:
        raise RateLimited(f"{what} 触发限频（HTTP 429）")
    if status >= 400:
        raise TransientError(f"{what} 失败（HTTP {status}）")


class MinerUClient:
    def __init__(self, token: str, base_url: str, transport: httpx.BaseTransport | None) -> None:
        self._token = token
        self._api = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=60,
            follow_redirects=False,
            transport=transport,
        )
        self._plain = httpx.Client(timeout=180, follow_redirects=True, transport=transport)

    def scrub(self, text: str) -> str:
        return scrub(text, self._token)

    def close(self) -> None:
        self._api.close()
        self._plain.close()

    def _call(self, method: str, path: str, **kwargs: Any) -> Mapping[str, Any]:
        try:
            response = self._api.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise TransientError(f"连接 MinerU 失败：{type(error).__name__}") from error
        _raise_for_status(response, "MinerU API")
        try:
            body: Mapping[str, Any] = response.json()
        except ValueError as error:
            raise SchemaChanged("MinerU 返回的不是 JSON") from error
        if body.get("code") != 0:
            _raise_for_code(body.get("code"), scrub(str(body.get("msg", "")), self._token))
        data = body.get("data")
        if not isinstance(data, Mapping):
            raise SchemaChanged("MinerU 返回缺少 data 字段")
        return data

    def request_upload(self, request: Mapping[str, Any]) -> tuple[str, str]:
        data = self._call("POST", "/api/v4/file-urls/batch", json=request)
        urls = data.get("file_urls")
        batch_id = data.get("batch_id")
        if not isinstance(batch_id, str) or not isinstance(urls, list) or len(urls) != 1:
            raise SchemaChanged("MinerU 申请上传链接的返回结构不符")
        return batch_id, str(urls[0])

    def upload(self, url: str, content: bytes) -> None:
        """签名链接上传：不能带 Content-Type，否则签名校验失败。"""
        try:
            response = self._plain.put(url, content=content)
        except httpx.HTTPError as error:
            raise TransientError(f"上传 PDF 失败：{type(error).__name__}") from error
        _raise_for_status(response, "上传 PDF")

    def batch_entry(self, batch_id: str) -> Mapping[str, Any]:
        data = self._call("GET", f"/api/v4/extract-results/batch/{batch_id}")
        entries = data.get("extract_result")
        if not isinstance(entries, list):
            raise SchemaChanged("MinerU 查询结果缺少 extract_result")
        if not entries:
            return {"state": "pending"}
        entry = entries[0]
        if not isinstance(entry, Mapping):
            raise SchemaChanged("MinerU 查询结果结构不符")
        return entry

    def download(self, url: str, max_bytes: int) -> bytes:
        if not url.startswith("https://"):
            raise SchemaChanged("MinerU 返回的结果链接不是 https，拒绝下载")
        try:
            with self._plain.stream("GET", url) as response:
                _raise_for_status(response, "下载解析结果")
                return _read_limited(response, max_bytes)
        except httpx.HTTPError as error:
            raise TransientError(f"下载解析结果失败：{type(error).__name__}") from error


def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise PermanentError(f"解析结果压缩包超过 {max_bytes // 1_000_000}MB，已停止下载")
        chunks.append(chunk)
    return b"".join(chunks)
