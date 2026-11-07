"""适配器共享层：HTTP 错误分类、限速、增量窗口、查询拼装、记录校验、PDF 安全下载。"""

from datetime import date

import httpx
import pytest
from tests.helpers.http import Router, public_resolver

from litgleaner.adapters.common.download import PdfDownloader, system_resolver
from litgleaner.adapters.common.http import HttpClient, user_agent
from litgleaner.adapters.common.query import boolean_query, matches_terms
from litgleaner.adapters.common.records import RecordBuilder, ensure_supported
from litgleaner.adapters.common.text import parse_date, strip_markup
from litgleaner.adapters.common.window import next_cursor, window_from_cursor
from litgleaner.domain.errors import (
    AuthFailed,
    RateLimited,
    SchemaChanged,
    TransientError,
)
from litgleaner.domain.query import AuthorWatch, KeywordSearch

PDF = b"%PDF-1.7\n" + b"x" * 100


def client(router: Router, **kwargs) -> HttpClient:
    return HttpClient(
        "Demo", base_url="https://api.example.org", transport=router.transport, **kwargs
    )


# ---- HTTP ----


def test_user_agent_includes_contact_only_when_given():
    assert "mailto:" not in user_agent(None)
    assert "mailto:me@example.org" in user_agent("me@example.org")


def test_get_json_returns_body_and_records_raw_page():
    router = Router().reply("/works", json_body={"ok": 1})
    http = client(router)
    assert http.get_json("/works", {"q": "x"}) == {"ok": 1}
    assert b"ok" in http.raw_log()
    assert router.requests[0].headers["user-agent"].startswith("litgleaner/")


@pytest.mark.parametrize(
    ("status", "error"),
    [(401, AuthFailed), (403, AuthFailed), (429, RateLimited), (500, TransientError)],
)
def test_status_codes_map_to_error_types(status: int, error: type[Exception]):
    http = client(Router().reply("/x", status))
    with pytest.raises(error):
        http.get_json("/x")


def test_bad_request_means_adapter_needs_fixing():
    with pytest.raises(SchemaChanged, match="400"):
        client(Router().reply("/x", 400)).get_json("/x")


def test_retry_after_header_sets_defer_time():
    http = client(Router().reply("/x", 429, headers={"Retry-After": "120"}))
    with pytest.raises(RateLimited) as caught:
        http.get_json("/x")
    assert caught.value.retry_after_seconds == 120


def test_not_found_can_be_treated_as_missing():
    assert client(Router()).get_json("/missing", allow_missing=True) is None


def test_non_json_body_is_schema_change():
    with pytest.raises(SchemaChanged):
        client(Router().reply("/x", body=b"<html>")).get_json("/x")


def test_network_error_is_transient():
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(TransientError, match="ConnectError"):
        client(Router().on("/x", broken)).get_json("/x")


def test_secrets_never_appear_in_errors_or_raw_log():
    router = Router().reply("/x", 500, body=b"bad key SECRET-KEY")
    http = client(router, secrets=("SECRET-KEY",))
    with pytest.raises(TransientError) as caught:
        http.get_json("/x", {"api_key": "SECRET-KEY"})
    assert "SECRET-KEY" not in str(caught.value)
    assert b"SECRET-KEY" not in http.raw_log()


def test_min_interval_spaces_requests():
    slept: list[float] = []
    now = iter([0.0, 0.2, 0.2])
    router = Router().reply("/x", json_body={})
    http = client(router, min_interval=1.0, monotonic=lambda: next(now), sleep=slept.append)
    http.get_json("/x")
    http.get_json("/x")
    assert slept == [pytest.approx(0.8)]


# ---- 增量窗口 ----


def test_first_run_looks_back_and_cursor_marks_today():
    window = window_from_cursor(None, date(2026, 11, 5), lookback_days=7, overlap_days=2)
    assert (window.since, window.until) == (date(2026, 10, 29), date(2026, 11, 5))
    assert next_cursor(window) == "2026-11-05"


def test_later_runs_overlap_to_catch_late_indexing():
    window = window_from_cursor("2026-11-04", date(2026, 11, 5), lookback_days=7, overlap_days=2)
    assert window.since == date(2026, 11, 2)


def test_unreadable_cursor_falls_back_to_lookback():
    window = window_from_cursor("page-3", date(2026, 11, 5), lookback_days=3, overlap_days=0)
    assert window.since == date(2026, 11, 2)


# ---- 查询 ----


def test_boolean_query_quotes_phrases_and_excludes():
    query = KeywordSearch(terms=("graph neural network", "GNN"), exclude=("survey",))
    assert boolean_query(query) == '("graph neural network" OR "GNN") NOT ("survey")'


def test_boolean_query_supports_field_syntax():
    query = KeywordSearch(terms=("a b",), exclude=())
    assert boolean_query(query, phrase=lambda t: f'all:"{t}"', not_op="ANDNOT") == '(all:"a b")'


def test_local_term_filter_for_apis_without_boolean_search():
    query = KeywordSearch(terms=("graph neural",), exclude=("survey",))
    assert matches_terms(query, "Graph  Neural nets", None)
    assert not matches_terms(query, "A survey of graph neural nets", None)
    assert not matches_terms(query, "Unrelated", "nothing here")


# ---- 记录 ----


def test_record_builder_skips_invalid_items_with_warning():
    builder = RecordBuilder("demo")
    builder.add(title="  ", doi="10.1/x")
    builder.add(title="Good", doi="10.1/y", external_ids={"pmid": "1", "empty": ""})
    assert [r.title for r in builder.records] == ["Good"]
    assert builder.records[0].external_ids == {"pmid": "1"}
    assert builder.warnings == ("demo：跳过 1 条无效记录",)


def test_ensure_supported_rejects_undeclared_query():
    with pytest.raises(ValueError, match="不支持"):
        ensure_supported(frozenset({KeywordSearch}), AuthorWatch(names=("A",)))


# ---- 文本 ----


def test_strip_markup_removes_jats_and_entities():
    raw = "<jats:p>Deep &amp; <jats:italic>wide</jats:italic>\n  nets</jats:p>"
    assert strip_markup(raw) == "Deep & wide nets"
    assert strip_markup("  ") is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-05", date(2026, 11, 5)),
        ("2026-11", date(2026, 11, 1)),
        ("2026", date(2026, 1, 1)),
        ([2026, 2], date(2026, 2, 1)),
        ("2026-02-30", None),
        ([None], None),
        ([2026, None], date(2026, 1, 1)),
        (["x"], None),
        (None, None),
        ("unknown", None),
    ],
)
def test_parse_date_accepts_partial_dates(value, expected):
    assert parse_date(value) == expected


# ---- PDF 下载 ----


def downloader(router: Router, **kwargs) -> PdfDownloader:
    return PdfDownloader(transport=router.transport, resolver=public_resolver, **kwargs)


def test_download_returns_pdf_bytes():
    router = Router().reply("files.example.org/a.pdf", body=PDF)
    assert downloader(router).fetch("https://files.example.org/a.pdf") == PDF


def test_download_ignores_html_landing_pages():
    router = Router().reply("/a", body=b"<html>login</html>")
    assert downloader(router).fetch("https://pub.example.org/a") is None


@pytest.mark.parametrize("status", [401, 403, 404, 410, 500])
def test_download_treats_refusals_as_unavailable(status: int):
    router = Router().reply("/a", status)
    assert downloader(router).fetch("https://pub.example.org/a") is None


def test_download_rate_limit_is_reported():
    router = Router().reply("/a", 429)
    with pytest.raises(RateLimited):
        downloader(router).fetch("https://pub.example.org/a")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.pdf",
        "http://10.0.0.5/a.pdf",
        "http://169.254.169.254/latest",
        "http://[::1]/a.pdf",
        "file:///etc/passwd",
        "ftp://example.org/a.pdf",
    ],
)
def test_download_refuses_private_and_non_http_targets(url: str):
    router = Router().reply("", body=PDF)
    assert downloader(router).fetch(url) is None
    assert router.requests == []


def test_download_checks_every_redirect_hop():
    router = (
        Router()
        .reply("pub.example.org", 302, headers={"Location": "http://127.0.0.1/secret.pdf"})
        .reply("127.0.0.1", body=PDF)
    )
    assert downloader(router).fetch("https://pub.example.org/a") is None
    assert len(router.requests) == 1


def test_download_follows_public_redirects():
    router = (
        Router()
        .reply("pub.example.org", 301, headers={"Location": "https://cdn.example.org/a.pdf"})
        .reply("cdn.example.org", body=PDF)
    )
    assert downloader(router).fetch("https://pub.example.org/a") == PDF


def test_download_stops_at_size_limit():
    router = Router().reply("/big", body=PDF + b"y" * 2000)
    assert downloader(router, max_bytes=1000).fetch("https://pub.example.org/big") is None


def test_download_sends_extra_headers_only_to_given_url():
    router = Router().reply("/a", body=PDF)
    downloader(router).fetch("https://pub.example.org/a", headers={"X-Token": "t"})
    assert router.requests[0].headers["x-token"] == "t"


def fake_ip_resolver(host: str) -> list[str]:
    """Clash、Surge、Shadowrocket 等代理的 fake-IP 模式：所有域名都解析到 198.18.0.0/15。"""
    return [host] if host[:1].isdigit() else ["198.18.2.237"]


def test_download_works_behind_fake_ip_proxy():
    router = Router().reply("www.nature.com", body=PDF)
    pdf = PdfDownloader(transport=router.transport, resolver=fake_ip_resolver)
    assert pdf.fetch("https://www.nature.com/a.pdf") == PDF


def test_fake_ip_range_can_be_disallowed_and_is_never_allowed_as_literal():
    router = Router().reply("", body=PDF)
    strict = PdfDownloader(
        transport=router.transport, resolver=fake_ip_resolver, allow_fake_ip=False
    )
    assert strict.fetch("https://www.nature.com/a.pdf") is None
    lenient = PdfDownloader(transport=router.transport, resolver=fake_ip_resolver)
    assert lenient.fetch("http://198.18.0.1/a.pdf") is None
    assert router.requests == []


def test_vendor_specific_retry_after_header_is_honoured():
    http = client(Router().reply("/x", 429, headers={"X-RateLimit-Retry-After": "60"}))
    with pytest.raises(RateLimited) as caught:
        http.get_json("/x")
    assert caught.value.retry_after_seconds == 60


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Gene &lt;i&gt;BRCA1&lt;/i&gt; study", "Gene BRCA1 study"),
        ("levels < 0.1 ug/L and > 5 units", "levels < 0.1 ug/L and > 5 units"),
        ("x &lt; 0.05 vs y", "x < 0.05 vs y"),
        ('<p class="a">CO<sub>2</sub> uptake</p>', "CO 2 uptake"),
    ],
)
def test_strip_markup_handles_escaped_tags_and_real_angle_brackets(raw: str, expected: str):
    assert strip_markup(raw) == expected


def test_download_gives_up_when_total_time_exceeds_deadline():
    ticks = iter([0.0, 10.0, 500.0, 900.0])
    router = Router().on(
        "/slow", lambda _: httpx.Response(200, content=iter([PDF[:20], PDF[20:40], PDF[40:]]))
    )
    pdf = PdfDownloader(
        transport=router.transport,
        resolver=public_resolver,
        deadline_seconds=180,
        monotonic=lambda: next(ticks),
    )
    assert pdf.fetch("https://pub.example.org/slow") is None


# ---- 安全评审补充 ----


def test_secret_headers_are_not_forwarded_on_cross_host_redirect():
    router = (
        Router()
        .reply("api.example.org/x", 302, headers={"Location": "https://other.example.net/y"})
        .reply("other.example.net/y", json_body={})
    )
    http = client(router, headers={"x-api-key": "SECRET-KEY"}, secrets=("SECRET-KEY",))
    http.get_json("/x")
    first, second = router.requests
    assert first.headers["x-api-key"] == "SECRET-KEY"
    assert "x-api-key" not in second.headers


def test_url_encoded_secrets_are_scrubbed():
    router = Router().reply("/x", json_body={})
    http = client(router, secrets=("a b&c",))
    http.get_json("/x", {"api_key": "a b&c"})
    assert b"a+b%26c" not in http.raw_log()
    assert b"a%20b%26c" not in http.raw_log()


def test_downloader_does_not_send_contact_email():
    router = Router().reply("/a", body=PDF)
    PdfDownloader(transport=router.transport, resolver=public_resolver).fetch(
        "https://pub.example.org/a"
    )
    assert "mailto" not in router.requests[0].headers["user-agent"]


@pytest.mark.parametrize(
    "host", ["localhost", "nas.lan", "printer.local", "db.internal", "intranet", "x.home.arpa"]
)
def test_fake_ip_exception_never_covers_internal_names(host: str):
    router = Router().reply("", body=PDF)
    pdf = PdfDownloader(transport=router.transport, resolver=fake_ip_resolver)
    assert pdf.fetch(f"http://{host}/a.pdf") is None
    assert router.requests == []


@pytest.mark.parametrize("address", ["64:ff9b::a9fe:a9fe", "192.0.0.9", "fec0::1"])
def test_translated_and_special_ranges_are_refused(address: str):
    router = Router().reply("", body=PDF)
    pdf = PdfDownloader(transport=router.transport, resolver=lambda _: [address])
    assert pdf.fetch("https://looks-public.example.org/a.pdf") is None


def test_redirect_cannot_downgrade_to_plain_http():
    router = (
        Router()
        .reply("https://pub.example.org", 302, headers={"Location": "http://cdn.example.org/a"})
        .reply("http://cdn.example.org", body=PDF)
    )
    assert downloader(router).fetch("https://pub.example.org/a") is None


def test_deadline_is_checked_between_redirect_hops():
    ticks = iter([0.0, 10.0, 500.0, 500.0])
    router = (
        Router()
        .reply("pub.example.org", 302, headers={"Location": "https://cdn.example.org/a"})
        .reply("cdn.example.org", body=PDF)
    )
    pdf = PdfDownloader(
        transport=router.transport,
        resolver=public_resolver,
        deadline_seconds=180,
        monotonic=lambda: next(ticks),
    )
    assert pdf.fetch("https://pub.example.org/a") is None
    assert len(router.requests) == 1


# ---- 代码评审补充 ----


def test_truncation_is_flagged_and_explained():
    builder = RecordBuilder("demo")
    assert not builder.truncated
    builder.truncate(500)
    assert builder.truncated
    assert "游标不前进" in builder.warnings[-1]


def test_records_with_same_doi_in_different_case_are_merged():
    builder = RecordBuilder("demo")
    builder.add(title="A paper", doi="10.1/ABC")
    builder.add(title="A paper", doi="10.1/abc")
    assert len(builder.records) == 1


@pytest.mark.parametrize("value", [123, ["<p>x</p>"], {"a": 1}])
def test_strip_markup_ignores_non_text(value):
    assert strip_markup(value) is None


@pytest.mark.parametrize("value", [2026, 3.5, {"year": 2026}])
def test_parse_date_ignores_unexpected_types(value):
    assert parse_date(value) is None


def test_retry_after_with_unicode_digits_falls_back_to_default():
    http = client(Router().reply("/x", 429, headers={"Retry-After": "²".encode("latin-1")}))
    with pytest.raises(RateLimited) as caught:
        http.get_json("/x")
    assert caught.value.retry_after_seconds > 0


@pytest.mark.parametrize("url", ["http://example..com/a.pdf", "http://[::1/a.pdf"])
def test_malformed_urls_are_unavailable_not_crashes(url: str):
    router = Router().reply("", body=PDF)
    assert PdfDownloader(transport=router.transport, resolver=system_resolver).fetch(url) is None
