"""Zotero Web API v3：信号源（InboxDrain）和全文来源。

录制数据来自 2026-11-05 对公开群组 2205533（Zotero 官方 “Test Items Library”）的真实匿名调用，
裁剪为几条并去掉了 links 等无关字段。以下内容按文档构造，未经真实调用验证：
附件下载 /items/{key}/file 的 302 重定向与 PDF 内容（公开群组不开放文件下载，实测 404）、
回收站条目与独立笔记（测试里在录制数据上追加的两条）、429/403 响应。
"""

import copy
from datetime import date
from typing import Any

import httpx
import pytest
from tests.helpers.http import Router, fixture_json, public_resolver

from litfactory.adapters.zotero.fulltext import ZoteroFullText
from litfactory.adapters.zotero.source import ZoteroSource
from litfactory.domain.errors import AuthFailed, ConfigError, RateLimited, SchemaChanged
from litfactory.domain.identity import Alias
from litfactory.domain.query import InboxDrain, JournalWatch
from litfactory.ports.fulltext import FullTextRequest

TODAY = date(2026, 5, 20)
KEY = "zotero-secret-key-123"
GROUP = "api.zotero.org/groups/2205533"
RECENT = fixture_json("zotero", "web_recent.json")
VERSIONS = fixture_json("zotero", "web_versions.json")
ITEMKEY = fixture_json("zotero", "web_itemkey.json")
HEADERS = {"Last-Modified-Version": "15205", "Total-Results": "1562"}
PDF = b"%PDF-1.6\nzotero cloud"


def source(router: Router, **options: Any) -> ZoteroSource:
    return ZoteroSource(
        "zotero",
        {"mode": "web", "group_id": 2205533, "lookback_days": 102, **options},
        contact_email=None,
        api_key=KEY,
        transport=router.transport,
        today=lambda: TODAY,
    )


def test_web_mode_declares_inbox_drain_and_requires_key():
    assert source(Router()).capabilities == frozenset({InboxDrain})
    with pytest.raises(ValueError, match="不支持"):
        source(Router()).fetch(JournalWatch(issns=("1234-5678",)), None)
    with pytest.raises(ConfigError, match="ZOTERO_API_KEY"):
        ZoteroSource("zotero", {"mode": "web", "user_id": 1}, contact_email=None)


def test_first_run_pages_newest_first_and_stops_at_lookback():
    router = Router().reply(f"{GROUP}/items/top", json_body=RECENT, headers=HEADERS)
    result = source(router).fetch(InboxDrain(), None)
    (sent,) = router.requests
    params = router.params(0)
    assert (params["sort"], params["direction"], params["itemType"]) == (
        "dateAdded",
        "desc",
        "-attachment",
    )
    assert params["start"] == "0"
    assert sent.headers["Zotero-API-Key"] == KEY
    assert sent.headers["Zotero-API-Version"] == "3"
    assert [r.title for r in result.records] == [
        "The scapegoat",
        "Urban segregation even on holiday: income differences in the choice of destinations",
        "The varieties of capitalism and hybrid success: Denmark in the global economy",
    ]
    assert result.next_cursor == "15205"
    assert any("跳过 1 条" in warning for warning in result.warnings)


def test_web_record_fields_are_mapped():
    router = Router().reply(f"{GROUP}/items/top", json_body=RECENT, headers=HEADERS)
    records = {
        r.external_ids["zotero"]: r for r in source(router).fetch(InboxDrain(), None).records
    }
    record = records["groups/2205533/TYG29UFR"]
    assert record.authors == ("John L. Campbell", "Ove K. Pedersen")
    assert record.doi == "10.1177/0010414006286542"
    assert (record.venue, record.issn) == ("Comparative Political Studies", "0010-4140")
    assert record.published_at == date(2007, 3, 1)
    assert record.abstract and record.abstract.startswith("The varieties of capitalism literature")
    assert records["groups/2205533/4PUL57BD"].published_at == date(1957, 1, 1)


def test_first_run_follows_pages_and_skips_notes_and_trash():
    page = copy.deepcopy(RECENT[:3])
    note_data = {"itemType": "note", "note": "x", "dateAdded": "2026-03-01T00:00:00Z"}
    note = {"key": "NOTE0001", "version": 1, "data": note_data}
    trashed = copy.deepcopy(RECENT[3])
    trashed["data"]["deleted"] = 1
    pages = [[*page[:2], note], [page[2], trashed]]

    def handler(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params["start"]) // 3
        body = pages[index] if index < len(pages) else []
        return httpx.Response(200, json=body, headers=HEADERS)

    router = Router().on(f"{GROUP}/items/top", handler)
    result = source(router, page_size=3).fetch(InboxDrain(), None)
    assert [request.url.params["start"] for request in router.requests] == ["0", "3"]
    assert [r.external_ids["zotero"] for r in result.records] == [
        "groups/2205533/4PUL57BD",
        "groups/2205533/ZD5CZHEH",
    ]


def test_incremental_run_uses_since_version_and_item_keys():
    router = (
        Router()
        .reply(f"{GROUP}/items/top", json_body=VERSIONS, headers=HEADERS)
        .reply(f"{GROUP}/items?", json_body=ITEMKEY, headers=HEADERS)
    )
    result = source(router).fetch(InboxDrain(), "15180")
    versions, items = router.params(0), router.params(1)
    assert (versions["since"], versions["format"]) == ("15180", "versions")
    assert items["itemKey"] == "5BI2HC2P,VM76QQZ6,HYEYQKKT,CMK4ZNS6"
    assert [r.title for r in result.records] == ["tintinnabulum", "Hidden brain"]
    assert result.next_cursor == "15205"
    assert result.raw and KEY.encode() not in result.raw


def test_incremental_truncation_keeps_version_order():
    router = (
        Router()
        .reply(f"{GROUP}/items/top", json_body=VERSIONS, headers=HEADERS)
        .reply(f"{GROUP}/items?", json_body=ITEMKEY, headers=HEADERS)
    )
    result = source(router, max_records=2).fetch(InboxDrain(), "15180")
    assert router.params(1)["itemKey"] == "5BI2HC2P,VM76QQZ6"
    assert result.next_cursor == "15183"
    assert any("上限" in warning for warning in result.warnings)


def test_collection_name_is_resolved_to_key():
    router = (
        Router()
        .reply(f"{GROUP}/collections?", json_body=fixture_json("zotero", "web_collections.json"))
        .reply(
            f"{GROUP}/collections/ECHCPXZN/items/top",
            json_body=fixture_json("zotero", "web_collection_items.json"),
            headers=HEADERS,
        )
    )
    result = source(router, collection="Zotero repository proposed", lookback_days=200).fetch(
        InboxDrain(), None
    )
    assert "Root mucilage" in result.records[0].title
    missing = Router().reply(f"{GROUP}/collections?", json_body=[])
    with pytest.raises(ConfigError, match="collection"):
        source(missing, collection="Nope").fetch(InboxDrain(), None)


def test_user_library_prefix_and_external_id():
    router = Router().reply(
        "api.zotero.org/users/123/items/top", json_body=RECENT[2:3], headers=HEADERS
    )
    adapter = ZoteroSource(
        "zotero",
        {"mode": "web", "user_id": 123, "lookback_days": 3650},
        contact_email=None,
        api_key=KEY,
        transport=router.transport,
    )
    (record,) = adapter.fetch(InboxDrain(), None).records
    assert record.external_ids["zotero"] == "users/123/ZD5CZHEH"


def test_web_errors():
    with pytest.raises(RateLimited):
        source(Router().reply("items/top", 429, headers={"Retry-After": "30"})).fetch(
            InboxDrain(), None
        )
    with pytest.raises(AuthFailed):
        source(Router().reply("items/top", 403, body=b"Forbidden")).fetch(InboxDrain(), None)
    with pytest.raises(SchemaChanged):
        source(Router().reply("items/top", json_body={"items": []})).fetch(InboxDrain(), None)
    with pytest.raises(SchemaChanged):
        source(Router().reply("items/top", json_body=[1, 2])).fetch(InboxDrain(), "15180")


# ---- 全文 ----

TITLE = "The quantitative bias: how to look at lots of books in context"
DOI = "10.1353/dph.2025.a959164"


def fulltext(router: Router) -> ZoteroFullText:
    return ZoteroFullText(
        {"mode": "web", "group_id": 2205533},
        contact_email=None,
        api_key=KEY,
        transport=router.transport,
        resolver=public_resolver,
    )


def library_router(file_handler: Any) -> Router:
    return (
        Router()
        .on(f"{GROUP}/items/QQB55B7Q/file", file_handler)
        .reply(
            f"{GROUP}/items/B9XBDFVB/children",
            json_body=fixture_json("zotero", "web_children.json"),
        )
        .reply(f"{GROUP}/items?", json_body=fixture_json("zotero", "web_title_search.json"))
        .reply("files.example-storage.org/", body=PDF)
    )


def redirect(_: httpx.Request) -> httpx.Response:
    return httpx.Response(302, headers={"Location": "https://files.example-storage.org/f/1"})


def test_web_fulltext_finds_pdf_by_title_search_and_doi():
    router = library_router(redirect)
    found = fulltext(router).fetch(FullTextRequest("P", TITLE, (Alias("doi", DOI),)))
    assert found is not None
    assert (found.content, found.license_basis, found.provider) == (PDF, "user_supplied", "zotero")
    search = router.params(0)
    assert (search["q"], search["itemType"]) == (TITLE, "-attachment")
    file_request, storage_request = router.requests[2], router.requests[3]
    assert file_request.headers["Zotero-API-Key"] == KEY
    assert "Zotero-API-Key" not in storage_request.headers


def test_web_fulltext_rejects_title_match_with_other_doi():
    router = library_router(redirect)
    request = FullTextRequest("P", TITLE, (Alias("doi", "10.9999/other"),))
    assert fulltext(router).fetch(request) is None
    assert len(router.requests) == 1


def test_web_fulltext_skips_non_pdf_and_missing_files():
    html = library_router(lambda _: httpx.Response(200, content=b"<html>login</html>"))
    request = FullTextRequest("P", TITLE, (Alias("doi", DOI),))
    assert fulltext(html).fetch(request) is None
    missing = library_router(lambda _: httpx.Response(404, content=b"Not found"))
    assert fulltext(missing).fetch(request) is None


def test_web_fulltext_needs_title_and_identifier():
    router = Router()
    assert fulltext(router).fetch(FullTextRequest("P", TITLE, ())) is None
    assert fulltext(router).fetch(FullTextRequest("P", " ", (Alias("doi", DOI),))) is None
    assert router.requests == []
