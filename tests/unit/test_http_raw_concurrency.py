"""同一同步 HTTP 客户端跨线程复用时，原始响应归档不得互相污染。"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.parse import quote, quote_plus

import httpx
import pytest

from litgleaner.adapters.common.http import HttpClient
from litgleaner.domain.errors import SchemaChanged, TransientError


def _pages(raw):
    return [json.loads(line) for line in raw.splitlines()]


@pytest.mark.parametrize("method", ["get", "post"])
def test_interleaved_resets_and_responses_keep_each_threads_own_raw_pages(method):
    a_requested, b_reset, a_archived = Event(), Event(), Event()

    def handler(request):
        if request.url.path == "/A":
            a_requested.set()
            assert b_reset.wait(5)
        else:
            assert a_archived.wait(5)
        return httpx.Response(200, json={"owner": request.url.path[1:]})

    client = HttpClient(
        "shared", base_url="https://example.test", transport=httpx.MockTransport(handler)
    )

    def run(owner):
        if owner == "B":
            assert a_requested.wait(5)
        client.reset_raw_log()
        if owner == "B":
            b_reset.set()
        if method == "get":
            client.get_json(f"/{owner}")
        else:
            client.post_json(f"/{owner}", {})
        raw = client.raw_log()
        if owner == "A":
            a_archived.set()
        return _pages(raw)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a_future, b_future = pool.submit(run, "A"), pool.submit(run, "B")
            a_pages, b_pages = a_future.result(timeout=10), b_future.result(timeout=10)
        assert [json.loads(page["body"])["owner"] for page in a_pages] == ["A"]
        assert [json.loads(page["body"])["owner"] for page in b_pages] == ["B"]
        assert client.raw_log() == b""
    finally:
        client.close()


def test_same_thread_collects_multiple_pages_and_reset_starts_fresh():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"path": request.url.path})
    )
    client = HttpClient("pages", base_url="https://example.test", transport=transport)
    try:
        assert client.raw_log() == b""
        client.get_json("/one")
        client.post_json("/two", {})
        assert [json.loads(page["body"])["path"] for page in _pages(client.raw_log())] == [
            "/one",
            "/two",
        ]
        client.reset_raw_log()
        assert client.raw_log() == b""
        client.get_json("/three")
        assert [json.loads(page["body"])["path"] for page in _pages(client.raw_log())] == ["/three"]
    finally:
        client.close()


def test_different_clients_on_same_thread_do_not_share_raw_buffers():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=request.url.path))
    first = HttpClient("first", base_url="https://example.test", transport=transport)
    second = HttpClient("second", base_url="https://example.test", transport=transport)
    try:
        first.get_text("/first")
        assert second.raw_log() == b""
        second.get_text("/second")
        assert [page["body"] for page in _pages(first.raw_log())] == ["/first"]
        first.reset_raw_log()
        assert [page["body"] for page in _pages(second.raw_log())] == ["/second"]
    finally:
        first.close()
        second.close()


def test_error_pages_are_still_archived_and_scrubbed_before_error_is_raised():
    secret = "test key&value"
    body = secret + " " + quote(secret, safe="") + " " + quote_plus(secret) + " " + "x" * 500
    transport = httpx.MockTransport(lambda _request: httpx.Response(500, text=body))
    client = HttpClient(
        "errors", base_url="https://example.test", secrets=(secret,), transport=transport
    )
    try:
        with pytest.raises(TransientError) as caught:
            client.get_text("/failure", {"api_key": secret})
        raw = client.raw_log()
        assert _pages(raw)[0]["status"] == 500
        assert _pages(raw)[0]["body"].endswith("x" * 500)
        assert len(str(caught.value)) < 250  # 错误消息仍只保留原来的 200 字符响应摘要。
        assert all(
            value not in raw.decode()
            for value in (secret, quote(secret, safe=""), quote_plus(secret))
        )
        assert secret not in str(caught.value)
    finally:
        client.close()


def test_non_json_and_allowed_missing_responses_remain_in_raw_log():
    def handler(request):
        return httpx.Response(404 if request.url.path == "/missing" else 200, text="original body")

    client = HttpClient(
        "original", base_url="https://example.test", transport=httpx.MockTransport(handler)
    )
    try:
        with pytest.raises(SchemaChanged):
            client.get_json("/not-json")
        assert client.get_json("/missing", allow_missing=True) is None
        assert [page["status"] for page in _pages(client.raw_log())] == [200, 404]
    finally:
        client.close()


def test_disabled_raw_log_stays_empty_with_or_without_reset():
    client = HttpClient(
        "no raw",
        base_url="https://example.test",
        keep_raw=False,
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )
    try:
        client.get_json("/one")
        client.reset_raw_log()
        client.post_json("/two", {})
        assert client.raw_log() == b""
    finally:
        client.close()
