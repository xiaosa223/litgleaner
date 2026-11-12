"""来源调用的原始输入缝隙；会话独立于旧 raw 日志及具体存储。"""

import base64
import codecs
import json
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date
from typing import Any, Literal, Protocol
from urllib.parse import quote, quote_plus

import httpx
from pydantic import BaseModel

from litgleaner.domain.errors import SchemaChanged
from litgleaner.ports.source_capture import CaptureSink

_ACTIVE: ContextVar[CaptureSink | None] = ContextVar("source_capture_inputs", default=None)
_FORMAT: ContextVar[str] = ContextVar("source_capture_format", default="auto")
_JSON_STRING = re.compile(r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"')
_HEADERS = frozenset(
    {
        "content-type",
        "content-language",
        "location",
        "last-modified-version",
        "retry-after",
        "x-ratelimit-retry-after",
    }
)


@contextmanager
def capture_inputs(sink: CaptureSink | None) -> Iterator[None]:
    token = _ACTIVE.set(sink)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def captured_today(today: Callable[[], date]) -> Callable[[], date]:
    def recorded() -> date:
        value = today()
        if (sink := _ACTIVE.get()) is not None:
            sink.append("today", _json_bytes(value.isoformat()))
        return value

    return recorded


def read_source_bytes(reader: Callable[[], bytes]) -> bytes:
    raw = reader()
    if (sink := _ACTIVE.get()) is not None:
        sink.append("file", raw)
    return raw


def configured_options(source: object) -> tuple[Mapping[str, Any], tuple[str, ...]] | None:
    """只读取已校验的选项，不解析类名或动态构造适配器。"""
    options = getattr(source, "_options", None)
    if not isinstance(options, BaseModel):
        return None
    effective = options.model_dump(mode="json")
    interval = getattr(getattr(source, "_http", None), "min_interval", None)
    if isinstance(interval, int | float) and not isinstance(interval, bool):
        effective["min_interval_seconds"] = interval
    return effective, tuple(sorted(options.model_fields_set))


def secret_variants(secrets: Sequence[str]) -> tuple[str, ...]:
    variants = {
        value
        for secret in secrets
        for value in (
            secret,
            quote(secret, safe=""),
            quote_plus(secret),
            json.dumps(secret, ensure_ascii=True)[1:-1],
            json.dumps(secret, ensure_ascii=False)[1:-1],
            json.dumps(secret, ensure_ascii=True)[1:-1].replace("/", "\\/"),
            json.dumps(secret, ensure_ascii=False)[1:-1].replace("/", "\\/"),
        )
        if value
    }
    return tuple(sorted(variants, key=lambda value: (-len(value), value)))


def _encoding(raw: bytes, declared: str | None = None) -> str:
    try:
        encoding = codecs.lookup(declared or json.detect_encoding(raw)).name
    except LookupError:
        encoding = "utf-8"
    if encoding == "utf-8-sig":
        return "utf-8"
    if encoding == "utf-16":
        return "utf-16-be" if raw.startswith(codecs.BOM_UTF16_BE) else "utf-16-le"
    if encoding == "utf-32":
        return "utf-32-be" if raw.startswith(codecs.BOM_UTF32_BE) else "utf-32-le"
    return encoding


@contextmanager
def capture_format(mode: Literal["json", "text", "xml"]) -> Iterator[None]:
    token = _FORMAT.set(mode)
    try:
        yield
    finally:
        _FORMAT.reset(token)


def _apply_patches(text: str, patches: list[tuple[int, int, str]]) -> str:
    merged: list[tuple[int, int, str]] = []
    for start, end, replacement in sorted(patches):
        if merged and start < merged[-1][1]:
            previous = merged[-1]
            merged[-1] = (previous[0], max(previous[1], end), "[secret]")
        else:
            merged.append((start, end, replacement))
    parts: list[str] = []
    cursor = 0
    for start, end, replacement in merged:
        parts.extend((text[cursor:start], replacement))
        cursor = end
    return "".join((*parts, text[cursor:]))


def scrub_text(text: str, secrets: Sequence[str]) -> str:
    """脱敏完整字符串 token，不解析业务对象或重写未命中的 JSON。"""
    if not secrets:
        return text
    pattern = re.compile("|".join(re.escape(value) for value in secrets))
    tokens = list(_JSON_STRING.finditer(text))
    patches: list[tuple[int, int, str]] = []
    for token in tokens:
        value = json.loads(token.group())
        changed = pattern.sub("[secret]", value)
        if changed != value:
            patches.append((token.start(), token.end(), json.dumps(changed, ensure_ascii=True)))
    index = 0
    for match in pattern.finditer(text):
        while index < len(tokens) and tokens[index].end() <= match.start():
            index += 1
        if index < len(tokens):
            token = tokens[index]
            if token.start() <= match.start() and match.end() <= token.end():
                continue
        patches.append((match.start(), match.end(), "[secret]"))
    return _apply_patches(text, patches) if patches else text


def _scrub_bytes(raw: bytes, secrets: Sequence[str], encoding: str) -> bytes:
    if not secrets:
        return raw
    boms = {
        "utf-8": codecs.BOM_UTF8,
        "utf-16-le": codecs.BOM_UTF16_LE,
        "utf-16-be": codecs.BOM_UTF16_BE,
        "utf-32-le": codecs.BOM_UTF32_LE,
        "utf-32-be": codecs.BOM_UTF32_BE,
    }
    bom = boms.get(encoding, b"")
    prefix = bom if bom and raw.startswith(bom) else b""
    body = raw[len(prefix) :]
    errors = "surrogatepass" if encoding.startswith(("utf-16", "utf-32")) else "surrogateescape"
    text = body.decode(encoding, errors=errors)
    changed = scrub_text(text, secrets)
    if changed == text:
        return raw
    if text.encode(encoding, errors=errors) != body:
        raise ValueError("response encoding cannot roundtrip")
    return prefix + changed.encode(encoding, errors=errors)


def _response_encoding(response: httpx.Response, body: bytes) -> str:
    mode = _FORMAT.get()
    if mode == "xml" and 200 <= response.status_code < 300:
        return _xml_encoding(body)
    json_media = "json" in response.headers.get("content-type", "").lower()
    parses_json = mode == "json" and 200 <= response.status_code < 300
    if mode != "text" and (parses_json or json_media or _looks_json(body)):
        return _encoding(body)
    return _encoding(body, response.charset_encoding)


def _xml_encoding(body: bytes) -> str:
    detected = _encoding(body)
    if detected != "utf-8" or body.startswith(codecs.BOM_UTF8):
        return detected
    declaration = re.match(
        rb"""^\s*<\?xml\b[^>]*\bencoding\s*=\s*["']([A-Za-z0-9_.-]+)["']""", body
    )
    return _encoding(body, declaration.group(1).decode("ascii")) if declaration else detected


def _looks_json(body: bytes) -> bool:
    try:
        text = body.decode(_encoding(body)).lstrip("\ufeff \t\r\n")
    except UnicodeError:
        return False
    return bool(text) and text[0] in '{["-0123456789tfn'


def scrub_response_text(response: httpx.Response, secrets: Sequence[str]) -> str:
    encoding = _response_encoding(response, response.content)
    try:
        body = _scrub_bytes(response.content, secrets, encoding)
    except (UnicodeError, ValueError):
        return "[unavailable: unsafe encoding]"
    return body.decode(encoding, errors="replace")


class _ReadStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream) -> None:
        self.stream = stream
        self.parts: list[bytes] = []

    def __iter__(self) -> Iterator[bytes]:
        for part in self.stream:
            self.parts.append(part)
            yield part

    def close(self) -> None:
        self.stream.close()


def _http_entry(
    response: httpx.Response,
    body: bytes,
    complete: bool,
    scrub: Callable[[str], str],
    secrets: Sequence[str],
) -> tuple[bytes, bool]:
    request = response.request
    body_encoding = "decoded"
    if not complete and response.headers.get("content-encoding", "identity") != "identity":
        # 未完成的压缩流无法可靠脱敏；不把仍含密钥的编码字节写入归档。
        body, body_encoding = b"", "omitted-encoded"
    try:
        saved_body = _scrub_bytes(body, secrets, _response_encoding(response, body))
    except (UnicodeError, ValueError):
        saved_body, complete, body_encoding = b"", False, "omitted-unsafe-encoding"
    entry = _json_bytes(
        {
            "schema": 1,
            "method": request.method,
            "url": scrub(str(request.url)),
            "request_body": base64.b64encode(
                _scrub_bytes(request.content, secrets, _encoding(request.content))
            ).decode(),
            "status": response.status_code,
            "headers": {
                name.lower(): scrub(value)
                for name, value in response.headers.multi_items()
                if name.lower() in _HEADERS
            },
            "body": base64.b64encode(saved_body).decode(),
            "complete": complete,
            "body_encoding": body_encoding,
        }
    )
    return entry, complete


def capture_http(
    response: httpx.Response, scrub: Callable[[str], str], secrets: Sequence[str]
) -> None:
    sink = _ACTIVE.get()
    if sink is None:
        return
    assert isinstance(response.stream, httpx.SyncByteStream)
    stream = _ReadStream(response.stream)
    response.stream = stream
    try:
        response.read()
    except httpx.HTTPError:
        partial = b"".join(stream.parts)
        stream.parts.clear()
        entry, _ = _http_entry(response, partial, False, scrub, secrets)
        sink.append("http", entry)
        raise
    stream.parts.clear()
    entry, complete = _http_entry(response, response.content, True, scrub, secrets)
    sink.append("http", entry)
    if not complete:
        raise SchemaChanged("来源响应无法安全脱敏留档，已停止解析")


class SqlRows(Protocol):
    def __iter__(self) -> Iterator[Sequence[Any]]: ...
    def fetchall(self) -> Sequence[Sequence[Any]]: ...


class SqlReader(Protocol):
    def execute(self, sql: str, parameters: Sequence[Any] = (), /) -> SqlRows: ...


class _Rows:
    def __init__(self, rows: Sequence[Sequence[Any]]) -> None:
        self.rows = rows

    def __iter__(self) -> Iterator[Sequence[Any]]:
        return iter(self.rows)

    def fetchall(self) -> Sequence[Sequence[Any]]:
        return self.rows


def _sql_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"type": "bytes", "base64": base64.b64encode(value).decode()}
    return value


class CapturedSqlReader:
    def __init__(self, reader: SqlReader) -> None:
        self.reader = reader

    def execute(self, sql: str, parameters: Sequence[Any] = (), /) -> SqlRows:
        cursor = self.reader.execute(sql, parameters)
        if (sink := _ACTIVE.get()) is None:
            return cursor
        rows = cursor.fetchall()
        sink.append(
            "sql",
            _json_bytes(
                {
                    "schema": 1,
                    "sql": sql,
                    "parameters": [_sql_value(value) for value in parameters],
                    "rows": [[_sql_value(value) for value in row] for row in rows],
                }
            ),
        )
        return _Rows(rows)
