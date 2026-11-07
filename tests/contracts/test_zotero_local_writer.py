"""写入本机 Zotero（本地 API）。

假服务器按 Zotero 10.0.7 自带的 server_localAPI.js 的规则构造（2026-11 核对源码）：
写请求缺 Zotero-Server-ID 返回 428、key 不对返回 401、同一个 Write-Token 再用返回 412、
上传分申请、发文件（multipart 的 file 字段）、登记三步。没有对真实 Zotero 发过写请求。
"""

import hashlib
import json
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from litgleaner.adapters.zotero.local_api import read_local_key, save_local_key
from litgleaner.adapters.zotero.writer_local import ZoteroLocalLibrary, creator, write_token
from litgleaner.domain.errors import AuthFailed, ConfigError, Deferred, TransientError
from litgleaner.domain.library import LibraryEntry, note_marker

SERVER_ID = "SRV123456789"
KEY = "localkey0123456789abcdef01234567"
PDF = b"%PDF-1.7\nhello zotero"
HEADERS = {"Zotero-Server-ID": SERVER_ID}


def entry(**overrides: Any) -> LibraryEntry:
    values: dict[str, Any] = {
        "key": "01PAPER",
        "title": "Graph Neural Networks for EEG",
        "authors": ("Ada Lovelace", "张三"),
        "venue": "IEEE TNNLS",
        "published_at": "2026-10-01",
        "doi": "10.1000/gnn-eeg",
        "arxiv_id": None,
        "abstract": "We study EEG.",
        "url": "https://doi.org/10.1000/gnn-eeg",
        "tags": ("必读",),
        "note_html": f"<p>好论文</p><p>{note_marker('01PAPER')}</p>",
        "pdf": PDF,
    }
    return LibraryEntry(**{**values, **overrides})


@dataclass
class FakeZotero:
    objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    collections: dict[str, dict[str, Any]] = field(default_factory=dict)
    tokens: set[str] = field(default_factory=set)
    uploads: dict[str, dict[str, str]] = field(default_factory=dict)
    files: dict[str, bytes] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    fail_upload: bool = False
    remember: bool = True
    counter: int = 0

    def new_key(self) -> str:
        self.counter += 1
        return f"K{self.counter:07d}"

    def add_item(self, data: dict[str, Any], version: int = 5) -> str:
        key = self.new_key()
        self.objects[key] = {"key": key, "version": version, "data": {**data, "key": key}}
        return key

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/api/local/uploads/"):
            return self.receive(request, path.rsplit("/", 1)[1])
        return self.guard(request) or self.route(request)

    def guard(self, request: httpx.Request) -> httpx.Response | None:
        if request.method == "GET":
            return None
        if request.headers.get("Zotero-Server-ID") is None:
            return httpx.Response(428, text="Zotero-Server-ID not provided")
        if request.url.path == "/api/local/authorize":
            return httpx.Response(200, json={"key": KEY, "remember": self.remember})
        if request.headers.get("Zotero-API-Key") != KEY:
            return httpx.Response(401, text="Invalid or expired API key")
        return None

    def route(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        listings = {
            "/api/users/0/collections": self.collections,
            "/api/users/0/items": self.objects,
        }
        if path == "/api/":
            return httpx.Response(200, text="Nothing to see here.", headers=HEADERS)
        if path in listings:
            store = listings[path]
            if method == "GET":
                return httpx.Response(200, json=list(store.values()), headers=HEADERS)
            return self.write(request, store)
        return self.item_route(request, path.split("/")[5])

    def item_route(self, request: httpx.Request, key: str) -> httpx.Response:
        path, method = request.url.path, request.method
        if path.endswith("/children"):
            kids = [o for o in self.objects.values() if o["data"].get("parentItem") == key]
            return httpx.Response(200, json=kids, headers=HEADERS)
        if path.endswith("/file"):
            return self.file(request, key)
        if "If-Unmodified-Since-Version" not in request.headers:
            return httpx.Response(428, text="If-Unmodified-Since-Version not provided")
        if method == "PATCH":
            self.objects[key]["data"].update(json.loads(request.content))
        else:
            del self.objects[key]
        return httpx.Response(204, headers=HEADERS)

    def write(self, request: httpx.Request, store: dict[str, dict[str, Any]]) -> httpx.Response:
        token = request.headers.get("Zotero-Write-Token")
        if token in self.tokens:
            return httpx.Response(412, text="Write token already used")
        (body,) = json.loads(request.content)
        key = self.new_key()
        store[key] = {"key": key, "version": 1, "data": {**body, "key": key}}
        if token:
            self.tokens.add(token)
        return httpx.Response(200, json={"successful": {"0": store[key]}, "failed": {}})

    def file(self, request: httpx.Request, key: str) -> httpx.Response:
        if request.headers.get("If-None-Match") != "*":
            return httpx.Response(428, text="If-Match/If-None-Match header not provided")
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if "upload" in form:
            upload = self.uploads.pop(form["upload"])
            self.files[key] = bytes.fromhex(upload["bytes"])
            return httpx.Response(204)
        upload_key = f"UP{key}"
        self.uploads[upload_key] = {**form, "item": key, "bytes": ""}
        url = f"http://127.0.0.1:23119/api/local/uploads/{upload_key}"
        return httpx.Response(200, json={"url": url, "uploadKey": upload_key, "params": {}})

    def receive(self, request: httpx.Request, upload_key: str) -> httpx.Response:
        if self.fail_upload:
            return httpx.Response(500, text="Upload failed: disk full")
        upload = self.uploads[upload_key]
        assert request.headers["Content-Type"].startswith("multipart/form-data")
        body = request.content
        start = body.index(b"%PDF")
        data = body[start : body.index(b"\r\n--", start)]
        if hashlib.md5(data, usedforsecurity=False).hexdigest() != upload["md5"]:
            return httpx.Response(400, text="File MD5 does not match expected")
        upload["bytes"] = data.hex()
        return httpx.Response(201)


def library(fake: FakeZotero, api_key: str | None = KEY, **options: Any) -> ZoteroLocalLibrary:
    return ZoteroLocalLibrary(
        options,
        api_key=api_key,
        transport=httpx.MockTransport(fake),
        clock=lambda: 1_780_000_000.5,
    )


def test_creates_item_in_new_collection_with_note_and_pdf():
    fake = FakeZotero()
    receipt = library(fake, tags=["拾穗", "litgleaner"]).save(entry())

    assert receipt.created and receipt.warnings == ()
    (collection,) = fake.collections.values()
    assert collection["data"]["name"] == "拾穗"
    item = fake.objects[receipt.item_key]["data"]
    assert item["itemType"] == "journalArticle"
    assert item["DOI"] == "10.1000/gnn-eeg"
    assert item["publicationTitle"] == "IEEE TNNLS"
    assert item["collections"] == [collection["key"]]
    assert [t["tag"] for t in item["tags"]] == ["拾穗", "litgleaner", "必读"]
    assert item["creators"] == [
        {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
        {"creatorType": "author", "name": "张三"},
    ]
    children = [o["data"] for o in fake.objects.values() if o["data"].get("parentItem")]
    assert {c["itemType"] for c in children} == {"note", "attachment"}
    (attachment_key,) = [
        o["key"] for o in fake.objects.values() if o["data"]["itemType"] == "attachment"
    ]
    assert fake.files[attachment_key] == PDF


def test_every_write_carries_server_id_and_key_and_mtime_is_milliseconds():
    fake = FakeZotero()
    library(fake).save(entry())
    writes = [r for r in fake.requests if r.method != "GET" and "uploads" not in r.url.path]
    assert writes and all(r.headers["Zotero-Server-ID"] == SERVER_ID for r in writes)
    assert all(r.headers["Zotero-API-Key"] == KEY for r in writes)
    grant = next(r for r in writes if r.url.path.endswith("/file") and b"md5" in r.content)
    form = parse_qs(grant.content.decode())
    assert form["mtime"] == ["1780000000500"]
    assert form["filesize"] == [str(len(PDF))]
    upload = next(r for r in fake.requests if "uploads" in r.url.path)
    assert "Zotero-API-Key" not in upload.headers


def test_existing_item_found_by_doi_is_merged_not_duplicated():
    fake = FakeZotero()
    fake.collections["C1"] = {"key": "C1", "version": 1, "data": {"name": "拾穗"}}
    # 标题写法不同但 DOI 相同（DOI 写在 extra 里），而且已经有 PDF 和我们的笔记。
    key = fake.add_item(
        {
            "itemType": "journalArticle",
            "title": "GNNs for EEG",
            "extra": "DOI: 10.1000/GNN-EEG",
            "collections": ["OTHER"],
            "tags": [{"tag": "mine"}],
        }
    )
    fake.add_item({"itemType": "attachment", "parentItem": key, "contentType": "application/pdf"})
    fake.add_item({"itemType": "note", "parentItem": key, "note": note_marker("01PAPER")})
    before = len(fake.objects)

    receipt = library(fake).save(entry())

    assert (receipt.item_key, receipt.created) == (key, False)
    assert len(fake.objects) == before
    data = fake.objects[key]["data"]
    assert data["collections"] == ["OTHER", "C1"]
    assert [t["tag"] for t in data["tags"]] == ["mine", "拾穗", "必读"]


def test_same_title_with_different_doi_is_a_different_paper():
    fake = FakeZotero()
    fake.add_item({"itemType": "journalArticle", "title": entry().title, "DOI": "10.1000/other"})
    assert library(fake).save(entry()).created


def test_retry_after_note_was_written_does_not_fail_or_duplicate():
    fake = FakeZotero()
    lib = library(fake, attach_pdf=False)
    first = lib.save(entry())
    notes = [o for o in fake.objects.values() if o["data"]["itemType"] == "note"]
    # 模拟 Zotero 重启后笔记标记仍在库里，以及 Write-Token 仍在缓存里两种情况。
    second = lib.save(entry())
    assert second.item_key == first.item_key and not second.created
    assert [o for o in fake.objects.values() if o["data"]["itemType"] == "note"] == notes


def test_failed_upload_removes_empty_attachment_and_warns():
    fake = FakeZotero(fail_upload=True)
    receipt = library(fake).save(entry())
    assert receipt.created
    assert receipt.warnings and "PDF 没有放进 Zotero" in receipt.warnings[0]
    assert not [o for o in fake.objects.values() if o["data"]["itemType"] == "attachment"]


def test_arxiv_without_venue_is_saved_as_preprint():
    fake = FakeZotero()
    receipt = library(fake).save(entry(venue=None, doi=None, arxiv_id="2401.01234", pdf=None))
    item = fake.objects[receipt.item_key]["data"]
    assert (item["itemType"], item["repository"], item["archiveID"]) == (
        "preprint",
        "arXiv",
        "arXiv:2401.01234",
    )
    assert "publicationTitle" not in item


def test_missing_or_rejected_key_is_auth_failed():
    with pytest.raises(AuthFailed, match="zotero authorize"):
        library(FakeZotero(), api_key=None).save(entry())
    with pytest.raises(AuthFailed, match="zotero authorize"):
        library(FakeZotero(), api_key="wrong").save(entry())


def test_zotero_not_running_is_deferred_not_failed():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    lib = ZoteroLocalLibrary({}, api_key=KEY, transport=httpx.MockTransport(refuse))
    with pytest.raises(Deferred, match="打开 Zotero"):
        lib.save(entry())


def test_local_api_disabled_is_reported():
    def disabled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Local API is not enabled")

    lib = ZoteroLocalLibrary({}, api_key=KEY, transport=httpx.MockTransport(disabled))
    with pytest.raises(AuthFailed, match="允许其他应用通信"):
        lib.probe()


def test_only_local_hosts_are_allowed():
    with pytest.raises(ConfigError, match="只能连本机"):
        ZoteroLocalLibrary({"base_url": "http://example.com:23119"}, api_key=KEY)


def test_authorize_requires_always_allow():
    assert library(FakeZotero()).authorize() == KEY
    with pytest.raises(AuthFailed, match="始终允许"):
        library(FakeZotero(remember=False)).authorize()


def test_key_file_is_private_and_env_wins(tmp_path: Path):
    path = tmp_path / "cfg" / "zotero-local-key"
    save_local_key(KEY, path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_local_key({}, path) == KEY
    assert read_local_key({"ZOTERO_LOCAL_API_KEY": "fromenv"}, path) == "fromenv"
    assert read_local_key({}, tmp_path / "missing") is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Lovelace, Ada", {"lastName": "Lovelace", "firstName": "Ada"}),
        ("Ada King Lovelace", {"firstName": "Ada King", "lastName": "Lovelace"}),
        ("Plato", {"name": "Plato"}),
        ("欧阳 修", {"name": "欧阳 修"}),
    ],
)
def test_creator_names(name: str, expected: dict[str, str]):
    assert creator(name) == {"creatorType": "author", **expected}


def test_note_whose_write_token_was_already_used_is_treated_as_written():
    fake = FakeZotero(tokens={write_token("note", "01PAPER")})
    receipt = library(fake, attach_pdf=False).save(entry())
    assert receipt.created
    assert not [o for o in fake.objects.values() if o["data"]["itemType"] == "note"]


def test_item_whose_write_token_was_used_but_is_not_found_is_retried_later():
    fake = FakeZotero(tokens={write_token("item", "01PAPER")})
    with pytest.raises(TransientError, match="稍后重试"):
        library(fake).save(entry())
