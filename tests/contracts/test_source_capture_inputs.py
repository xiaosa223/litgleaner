"""响应在任何解析或状态判断前持久化，且保留字节与调用隔离。"""

import base64
import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Barrier
from urllib.parse import quote, quote_plus

import httpx
import pytest

from litgleaner.adapters.common.capture import capture_inputs
from litgleaner.adapters.common.http import HttpClient
from litgleaner.domain.errors import AuthFailed, RateLimited, SchemaChanged, TransientError


class Sink:
    def __init__(self):
        self.events = []

    def append(self, kind, content):
        self.events.append((kind, content))

    def pages(self):
        return [json.loads(content) for kind, content in self.events if kind == "http"]


def client(handler, **kwargs):
    return HttpClient(
        "capture", base_url="https://source.test", transport=httpx.MockTransport(handler), **kwargs
    )


@pytest.mark.parametrize(
    ("status", "error"), [(401, AuthFailed), (429, RateLimited), (500, TransientError)]
)
def test_error_response_is_saved_before_status_handling(status, error):
    sink = Sink()
    http = client(lambda _: httpx.Response(status, text="original", headers={"Retry-After": "17"}))
    with capture_inputs(sink), pytest.raises(error):
        http.get_json("/one")
    assert sink.pages()[0]["status"] == status
    assert base64.b64decode(sink.pages()[0]["body"]) == b"original"
    assert sink.pages()[0]["headers"]["retry-after"] == "17"
    http.close()


def test_json_failure_and_allowed_missing_are_both_saved():
    sink = Sink()
    http = client(
        lambda req: httpx.Response(404 if req.url.path == "/missing" else 200, text="bad")
    )
    with capture_inputs(sink):
        with pytest.raises(SchemaChanged):
            http.get_json("/invalid")
        assert http.get_json("/missing", allow_missing=True) is None
    assert [page["status"] for page in sink.pages()] == [200, 404]
    http.close()


def test_sink_failure_stops_response_before_json_parser(monkeypatch):
    class FailedSink:
        def append(self, kind, content):
            raise OSError("archive unavailable")

    parsed = []
    monkeypatch.setattr(httpx.Response, "json", lambda _: parsed.append(True))
    http = client(lambda _: httpx.Response(200, text="{}"))
    with capture_inputs(FailedSink()), pytest.raises(OSError, match="archive unavailable"):
        http.get_json("/one")
    assert parsed == []
    http.close()


def test_redirect_hops_and_necessary_headers_are_saved_without_credentials():
    sink = Sink()

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/end", "Set-Cookie": "private"})
        return httpx.Response(200, json={}, headers={"Last-Modified-Version": "102"})

    http = client(handler, headers={"Authorization": "Bearer private"}, secrets=("private",))
    with capture_inputs(sink):
        http.get_json("/start")
    pages = sink.pages()
    assert [page["status"] for page in pages] == [302, 200]
    assert pages[0]["headers"] == {"location": "/end"}
    assert pages[1]["headers"]["last-modified-version"] == "102"
    assert "private" not in repr(pages)
    assert all("request_headers" not in page for page in pages)
    http.close()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32", "iso-8859-1"])
def test_capture_preserves_original_json_bytes_and_encoding(encoding):
    raw = json.dumps({"title": "caf\u00e9"}, ensure_ascii=False).encode(encoding)
    sink = Sink()
    http = client(
        lambda _: httpx.Response(
            200, content=raw, headers={"Content-Type": f"application/json; charset={encoding}"}
        )
    )
    with capture_inputs(sink):
        response = http.get("/one")
    assert response.content == raw
    assert base64.b64decode(sink.pages()[0]["body"]) == raw
    assert sink.pages()[0]["headers"]["content-type"].endswith(encoding)
    http.close()


def test_compressed_response_is_saved_decoded_without_compression_headers():
    raw = b'{"title":"decoded once"}'
    sink = Sink()
    http = client(
        lambda _: httpx.Response(
            200,
            content=gzip.compress(raw),
            headers={"Content-Encoding": "gzip", "Content-Length": "123"},
        )
    )
    with capture_inputs(sink):
        assert http.get_json("/one") == {"title": "decoded once"}
    page = sink.pages()[0]
    assert base64.b64decode(page["body"]) == raw
    assert "content-encoding" not in page["headers"]
    assert "content-length" not in page["headers"]
    http.close()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be", "utf-32-le"])
def test_post_request_and_response_secrets_are_scrubbed_in_all_forms(encoding):
    secret = 'key/with "quote"\\ and\u79d8\u5bc6'
    variants = [
        secret,
        quote(secret, safe=""),
        quote_plus(secret),
        json.dumps(secret, ensure_ascii=True)[1:-1],
        json.dumps(secret, ensure_ascii=False)[1:-1],
    ]
    body = "|".join(variants).encode(encoding)
    sink = Sink()
    http = client(lambda _: httpx.Response(200, content=body), secrets=(secret,))
    with capture_inputs(sink), pytest.raises(SchemaChanged):
        http.post_json("/one?key=" + quote(secret, safe=""), {"key": secret})
    page = sink.pages()[0]
    saved = base64.b64decode(page["body"])
    assert all(value.encode(encoding) not in saved for value in variants)
    assert "[secret]".encode(encoding) in saved
    assert secret not in page["url"] and quote(secret, safe="") not in page["url"]
    assert secret.encode() not in base64.b64decode(page["request_body"])
    assert page["method"] == "POST"
    http.close()


def test_reset_raw_log_does_not_reset_durable_capture():
    sink = Sink()
    http = client(lambda req: httpx.Response(200, text=req.url.path))
    with capture_inputs(sink):
        http.get_text("/one")
        http.reset_raw_log()
        http.get_text("/two")
    assert [page["url"].rsplit("/", 1)[-1] for page in sink.pages()] == ["one", "two"]
    assert len(http.raw_log().splitlines()) == 1
    http.close()


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be", "utf-32-le"])
def test_short_secrets_do_not_corrupt_multibyte_character_boundaries(encoding):
    sink = Sink()
    http = client(
        lambda _: httpx.Response(
            200,
            content="e X 时".encode(encoding),
            headers={"Content-Type": f"text/plain; charset={encoding}"},
        ),
        secrets=("e",),
    )
    with capture_inputs(sink):
        http.get("/one")
    assert base64.b64decode(sink.pages()[0]["body"]).decode(encoding) == "[secret] X 时"
    http.close()


@pytest.mark.parametrize(
    ("encoding", "text"),
    [
        ("utf-16-le", "攀Ā"),
        ("utf-16-be", "Ā攀"),
        ("utf-32-le", "攀Ā"),
        ("utf-32-be", "Ā攀"),
        ("utf-16", "攀Ā"),
        ("utf-32", "攀Ā"),
    ],
)
def test_byte_pattern_crossing_code_unit_boundaries_does_not_change_original(encoding, text):
    raw = text.encode(encoding)
    sink = Sink()
    http = client(
        lambda _: httpx.Response(
            200, content=raw, headers={"Content-Type": f"text/plain; charset={encoding}"}
        ),
        secrets=("e",),
    )
    with capture_inputs(sink):
        http.get("/one")
    assert base64.b64decode(sink.pages()[0]["body"]) == raw
    http.close()


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-be", "utf-32", "utf-32-be", "utf-8-sig"])
def test_non_bmp_secret_is_removed_without_changing_bom_or_other_code_points(encoding):
    raw = "原🔐🧪文".encode(encoding)
    sink = Sink()
    http = client(
        lambda _: httpx.Response(
            200, content=raw, headers={"Content-Type": f"text/plain; charset={encoding}"}
        ),
        secrets=("🔐",),
    )
    with capture_inputs(sink):
        http.get("/one")
    assert base64.b64decode(sink.pages()[0]["body"]) == "原[secret]🧪文".encode(encoding)
    http.close()


def test_nested_contexts_restore_outer_and_do_not_leak_after_exit():
    first, second = Sink(), Sink()
    http = client(lambda _: httpx.Response(200, json={}))
    with capture_inputs(first):
        http.get_json("/outer-before")
        with capture_inputs(second):
            http.get_json("/inner")
        http.get_json("/outer-after")
    http.get_json("/unrecorded")
    assert len(first.pages()) == 2
    assert len(second.pages()) == 1
    assert all("inner" not in page["url"] for page in first.pages())
    http.close()


def test_concurrent_capture_scopes_do_not_mix_shared_http_client():
    barrier = Barrier(2)

    def handler(request):
        barrier.wait(5)
        return httpx.Response(200, text=request.url.path)

    http = client(handler)

    def run(owner):
        sink = Sink()
        with capture_inputs(sink):
            http.get_text(f"/{owner}")
        return sink.pages()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("first", "second")))
    assert [base64.b64decode(pages[0]["body"]) for pages in results] == [b"/first", b"/second"]
    http.close()


def test_fulltext_and_streaming_clients_do_not_capture():
    sink = Sink()
    http = client(lambda _: httpx.Response(200, text="one\ntwo\n"), keep_raw=False)
    stream = client(lambda _: httpx.Response(200, text="one\ntwo\n"))
    with capture_inputs(sink):
        http.get_text("/document")
        assert list(stream.post_lines("/stream", {})) == ["one", "two"]
    assert sink.events == []
    http.close()
    stream.close()


def test_broken_response_stream_retains_received_wire_bytes_as_incomplete():
    class BrokenStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b"partial bytes"
            raise httpx.ReadError("connection lost")

    sink = Sink()
    http = client(lambda _: httpx.Response(200, stream=BrokenStream()))
    with capture_inputs(sink), pytest.raises(TransientError):
        http.get_json("/broken")
    (page,) = sink.pages()
    assert page["complete"] is False
    assert page["body_encoding"] == "decoded"
    assert base64.b64decode(page["body"]) == b"partial bytes"
    assert "connection lost" not in repr(page)
    http.close()


def test_streaming_lines_survive_each_step_running_in_a_fresh_context():
    """Starlette 在线程池里逐次 next() 同步流式生成器，每次都在新的上下文副本里执行。"""
    sink = Sink()
    stream = client(lambda _: httpx.Response(200, text="one\ntwo\n"))
    with capture_inputs(sink):
        lines = stream.post_lines("/stream", {})
        received = []
        while True:
            try:
                received.append(copy_context().run(next, lines))
            except StopIteration:
                break
    assert received == ["one", "two"]
    assert sink.events == []
    stream.close()
