"""脱敏按字符与 JSON 字符串值工作，不破坏编码边界或遗留 raw。"""

import base64
import json
import xml.etree.ElementTree as ET

import httpx
import pytest

from litgleaner.adapters.common.capture import capture_format, capture_inputs, configured_options
from litgleaner.adapters.common.http import HttpClient
from litgleaner.adapters.sources.pubmed import PubMedSource
from litgleaner.adapters.sources.semantic_scholar import SemanticScholarSource
from litgleaner.adapters.zotero.source import ZoteroSource
from litgleaner.domain.errors import AuthFailed, SchemaChanged
from litgleaner.domain.query import InboxDrain


class Sink:
    def __init__(self):
        self.events = []

    def append(self, kind, content):
        self.events.append((kind, content))

    def page(self):
        return json.loads(self.events[0][1])


def client(body, secret, headers=None, status=200):
    return HttpClient(
        "redaction",
        base_url="https://source.test",
        secrets=(secret,),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, content=body, headers=headers)
        ),
    )


@pytest.mark.parametrize(
    "literal",
    [
        '"unit\\/秘密"',
        r'"\u0075nit\/\u79D8密"',
        r'"u\u006Eit\/秘\u5Bc6"',
        r'"\u0075\u006e\u0069\u0074\/\u79d8\u5bc6"',
    ],
)
@pytest.mark.parametrize("media_type", ["application/json", "text/plain"])
def test_json_mixed_escapes_are_removed_from_new_and_legacy_archive(literal, media_type):
    secret = "unit/秘密"
    raw = ('{"echo":' + literal + ',"other":"unchanged"}').encode()
    assert json.loads(raw)["echo"] == secret
    http = client(raw, secret, {"Content-Type": f"{media_type}; charset=iso-8859-1"})
    sink = Sink()
    with capture_inputs(sink):
        assert http.get_json("/one")["echo"] == secret
    saved = base64.b64decode(sink.page()["body"])
    assert json.loads(saved) == {"echo": "[secret]", "other": "unchanged"}
    legacy = json.loads(http.raw_log())["body"]
    assert json.loads(legacy)["echo"] == "[secret]"
    assert secret not in json.loads(legacy).values()
    http.close()


def test_error_details_also_mask_recoverable_json_escapes():
    secret = "unit/秘密"
    raw = r'{"echo":"\u0075nit\/\u79D8密"}'.encode()
    http = client(raw, secret, {"Content-Type": "application/json; charset=iso-8859-1"}, 401)
    sink = Sink()
    with capture_inputs(sink), pytest.raises(AuthFailed) as caught:
        http.get_json("/one")
    assert "[secret]" in str(caught.value)
    assert r"\u0075" not in str(caught.value)
    assert json.loads(json.loads(http.raw_log())["body"])["echo"] == "[secret]"
    http.close()


@pytest.mark.parametrize("status", [302, 401])
def test_intermediate_non_json_response_uses_its_own_charset(status):
    secret = "unit/café"
    body = f"<html>{secret}</html>".encode("iso-8859-1")

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(
                status,
                content=body,
                headers={"Content-Type": "text/html; charset=iso-8859-1", "Location": "/end"},
            )
        return httpx.Response(200, json={})

    http = HttpClient(
        "redirect",
        base_url="https://source.test",
        secrets=(secret,),
        transport=httpx.MockTransport(handler),
    )
    sink = Sink()
    with capture_inputs(sink):
        if status == 302:
            assert http.get_json("/start") == {}
        else:
            with pytest.raises(AuthFailed) as caught:
                http.get_json("/start")
            assert secret not in str(caught.value)
    saved = base64.b64decode(sink.page()["body"]).decode("iso-8859-1")
    assert saved == "<html>[secret]</html>"
    legacy = [json.loads(line) for line in http.raw_log().splitlines()]
    assert all(secret not in item["body"] for item in legacy)
    http.close()


def test_invalid_parent_json_and_unrelated_tokens_remain_byte_exact():
    literal = r'"\u0075nit\/\u79D8密"'.encode()
    raw = b'  {"keep" : "\\u0061", "echo":' + literal + b', "keep": 7, BROKEN'
    http = client(raw, "unit/秘密")
    sink = Sink()
    with capture_inputs(sink), pytest.raises(SchemaChanged):
        http.get_json("/one")
    saved = base64.b64decode(sink.page()["body"])
    assert saved == raw.replace(literal, b'"[secret]"')
    http.close()


@pytest.mark.parametrize(
    ("encoding", "text", "secret"),
    [("shift_jis", "パ", "p"), ("cp932", "パ", "p"), ("big5", "一", "@")],
)
def test_legacy_multibyte_trailing_byte_is_not_an_ascii_secret(encoding, text, secret):
    raw = text.encode(encoding)
    http = client(raw, secret, {"Content-Type": f"text/plain; charset={encoding}"})
    sink = Sink()
    with capture_inputs(sink):
        assert http.get_text("/one") == text
    assert base64.b64decode(sink.page()["body"]) == raw
    assert json.loads(http.raw_log())["body"] == text
    http.close()


def test_invalid_utf8_bytes_survive_around_redacted_tokens():
    raw = b"\xff " + rb'{"echo":"unit\/\u79D8\u5Bc6"}'
    http = client(raw, "unit/秘密")
    sink = Sink()
    with capture_inputs(sink), pytest.raises(SchemaChanged):
        http.get_json("/one")
    assert base64.b64decode(sink.page()["body"]) == b'\xff {"echo":"[secret]"}'
    assert sink.page()["complete"] is True
    http.close()


def test_invalid_json_tail_does_not_switch_to_misleading_charset():
    secret = "unit/秘密"
    raw = json.dumps({"echo": secret}, ensure_ascii=False).encode() + b"\xff"
    http = client(raw, secret, {"Content-Type": "application/json; charset=iso-8859-1"})
    sink = Sink()
    with capture_inputs(sink), pytest.raises(SchemaChanged):
        http.get_json("/one")
    saved = base64.b64decode(sink.page()["body"])
    assert saved == raw.replace(secret.encode(), b"[secret]")
    assert secret not in json.loads(http.raw_log())["body"]
    http.close()


def test_unsafe_roundtrip_stores_diagnostic_and_stops_before_parsing():
    secret = "{{SECRET_ixf00dlx}}"
    raw = b"\x87\x90 " + secret.encode()
    http = client(raw, secret, {"Content-Type": "text/plain; charset=cp932"})
    sink = Sink()
    with capture_inputs(sink), pytest.raises(SchemaChanged, match="无法安全脱敏留档"):
        http.get_text("/one")
    page = sink.page()
    assert page["complete"] is False
    assert page["body_encoding"] == "omitted-unsafe-encoding"
    assert page["body"] == ""
    assert secret not in repr(page)
    assert http.raw_log() == b""
    http.close()


def test_source_direct_get_json_uses_byte_encoding_despite_wrong_charset():
    secret = "{{SECRET_0s61cnlz}}"

    def handler(request):
        payload = {secret: 1} if request.url.params.get("format") == "versions" else []
        return httpx.Response(
            200,
            content=json.dumps(payload).encode("utf-16-le"),
            headers={
                "Content-Type": "text/plain; charset=iso-8859-1",
                "Last-Modified-Version": "1",
            },
        )

    source = ZoteroSource(
        "library",
        {"mode": "web", "user_id": 1, "lookback_days": None, "min_interval_seconds": 0},
        contact_email=None,
        api_key=secret,
        transport=httpx.MockTransport(handler),
    )
    sink = Sink()
    with capture_inputs(sink):
        result = source.fetch(InboxDrain(), "0")
    assert result.next_cursor == "1"
    assert result.records == ()
    assert secret not in json.loads(base64.b64decode(sink.page()["body"]))
    legacy = json.loads(result.raw.splitlines()[0])
    assert secret not in json.loads(legacy["body"])
    source._web.close()


@pytest.mark.parametrize("encoding", ["utf-16", "iso-8859-1"])
def test_raw_xml_parser_uses_xml_byte_encoding(encoding):
    secret = "unit/café"
    declaration = f'<?xml version="1.0" encoding="{encoding}"?>'
    raw = (declaration + f"<root>{secret}</root>").encode(encoding)
    http = client(raw, secret, {"Content-Type": "application/xml; charset=utf-8"})
    sink = Sink()
    with capture_inputs(sink), capture_format("xml"):
        response = http.get("/one")
    assert ET.fromstring(response.content).text == secret
    saved = base64.b64decode(sink.page()["body"])
    assert ET.fromstring(saved).text == "[secret]"
    http.close()


@pytest.mark.parametrize(
    ("factory", "key", "expected"),
    [
        (SemanticScholarSource, None, 3.0),
        (SemanticScholarSource, "{{SECRET_414ttyyy}}", 1.0),
        (PubMedSource, None, 0.34),
        (PubMedSource, "{{SECRET_414ttyyy}}", 0.1),
    ],
)
def test_recipe_uses_actual_authenticated_or_anonymous_interval(factory, key, expected):
    source = factory("source", {}, contact_email=None, api_key=key)
    effective, fields = configured_options(source)
    assert effective["min_interval_seconds"] == source._http.min_interval == expected
    assert fields == ()
    source._http.close()
