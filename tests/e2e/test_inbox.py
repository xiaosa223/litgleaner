"""添加论文（第 12.8 节）：贴标识符或上传 PDF，不要求通过初筛就取全文、解析、提取。"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient

from litgleaner.application import inbox
from litgleaner.application.stages.fulltext import AcquireStage
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.inbox import InboxItem
from litgleaner.ports.fulltext import FullText
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, paper

PDF = b'%PDF-1.7\n{"title": "Uploaded"}'  # 假解析器从第二行读标题


@pytest.fixture
def harness_with_offtopic(harness: Harness) -> Harness:
    # 初筛判为无关的论文：流水线不会去取它的全文
    harness.write_papers([paper("Off Topic Paper", 3, marker=" [[irrelevant]]")])
    return harness


def test_add_doi_resolves_metadata_and_reads_fulltext_without_triage(harness_with_offtopic):
    h = harness_with_offtopic
    item = inbox.add_identifier(h.ctx, "https://doi.org/10.1000/off-topic-paper")
    assert item.status == "done" and item.paper_id
    found = h.ctx.store.papers.get(item.paper_id)
    assert found is not None and found.title == "Off Topic Paper"
    assert h.ctx.store.reader.get_state(item.paper_id).status == "queued"  # 进入文库：待读

    h.run()
    state = h.ctx.store.papers.get_fulltext(item.paper_id)
    assert state is not None and state.status == "parsed"  # 没过初筛也取了全文并解析
    assert h.ctx.store.papers.get_extraction(item.paper_id) is not None


def test_add_same_doi_twice_reuses_the_paper(harness_with_offtopic):
    first = inbox.add_identifier(harness_with_offtopic.ctx, "10.1000/off-topic-paper")
    second = inbox.add_identifier(harness_with_offtopic.ctx, "doi:10.1000/off-topic-paper")
    assert first.paper_id == second.paper_id


def test_existing_identifier_can_be_added_without_network(harness_with_offtopic):
    h = harness_with_offtopic
    first = inbox.add_identifier(h.ctx, "10.1000/off-topic-paper")
    offline = replace(h.ctx, sources={})
    second = inbox.add_identifier(offline, "10.1000/off-topic-paper")
    assert second.status == "done" and first.paper_id == second.paper_id


def test_concurrent_doi_additions_do_not_create_duplicate_papers(
    harness_with_offtopic, monkeypatch
):
    h = harness_with_offtopic
    source = next(iter(h.ctx.sources.values()))
    fetch = source.fetch
    ready = Barrier(2)

    def together(query, cursor):
        result = fetch(query, cursor)
        ready.wait(timeout=5)
        return result

    monkeypatch.setattr(source, "fetch", together)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(lambda _: inbox.add_identifier(h.ctx, "10.1000/off-topic-paper"), range(2))
        )
    assert results[0].paper_id == results[1].paper_id
    assert h.ctx.store.stats()["paper"] == 1


def test_concurrent_pdf_uploads_do_not_create_duplicate_papers(harness: Harness):
    ready = Barrier(2)

    def upload(_index):
        ready.wait(timeout=5)
        return inbox.add_pdf(harness.ctx, "same.pdf", PDF, identifier=None)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(upload, range(2)))
    assert results[0].paper_id == results[1].paper_id
    assert harness.ctx.store.stats()["paper"] == 1


def test_inbox_wakeup_changes_only_when_metadata_is_ready(harness: Harness):
    repo = harness.ctx.store.inbox
    before = repo.ready_revision()
    item = InboxItem("waiting", "doi", "10.1000/waiting", "pending", harness.ctx.now_iso())
    repo.add(item)
    assert repo.ready_revision() == before
    repo.finish(item.id, "done", "paper", None, harness.ctx.now_iso())
    assert repo.ready_revision() != before


@pytest.mark.parametrize("found", [None, FullText(b"%PDF-1.7\nother", "open", "slow")])
def test_background_acquire_cannot_replace_a_concurrent_user_upload(harness_with_offtopic, found):
    h = harness_with_offtopic
    item = inbox.add_identifier(h.ctx, "10.1000/off-topic-paper")
    started, finish = Event(), Event()

    class SlowProvider:
        name = "slow"

        def fetch(self, _request):
            started.set()
            assert finish.wait(5)
            return found

    ctx = replace(h.ctx, fulltext=(SlowProvider(),))
    with ThreadPoolExecutor(max_workers=1) as workers:
        task = workers.submit(AcquireStage().process, ctx, {"paper_id": item.paper_id})
        try:
            assert started.wait(5)
            uploaded = inbox.add_pdf(h.ctx, "mine.pdf", PDF, "10.1000/off-topic-paper")
            assert uploaded.paper_id == item.paper_id
        finally:
            finish.set()
        task.result(timeout=5)
    state = h.ctx.store.papers.get_fulltext(item.paper_id)
    assert state.provider == "user" and state.pdf_sha256
    AcquireStage().on_dead(ctx, {"paper_id": item.paper_id})
    assert h.ctx.store.papers.get_fulltext(item.paper_id) == state


def test_unknown_identifier_fails_with_reason(harness: Harness):
    harness.write_papers([])
    item = inbox.add_identifier(harness.ctx, "10.1000/does-not-exist")
    assert item.status == "failed"
    assert item.error and "查不到" in item.error
    assert item.paper_id is None


def test_unrecognized_text_is_rejected(harness: Harness):
    with pytest.raises(InvalidRequest, match="认不出"):
        inbox.add_identifier(harness.ctx, "attention is all you need")


def test_plain_link_is_not_supported_yet(harness: Harness):
    with pytest.raises(InvalidRequest, match="链接"):
        inbox.add_identifier(harness.ctx, "https://example.org/some/paper")


def test_upload_pdf_with_identifier_from_first_page(harness_with_offtopic):
    h = harness_with_offtopic
    item = inbox.add_pdf(h.ctx, "paper.pdf", PDF, identifier="10.1000/off-topic-paper")
    assert item.status == "done" and item.paper_id
    state = h.ctx.store.papers.get_fulltext(item.paper_id)
    assert state is not None and state.status == "available"
    assert state.license_basis == "user_supplied"
    assert h.ctx.store.papers.get(item.paper_id).title == "Off Topic Paper"


def test_upload_pdf_with_unknown_identifier_says_what_was_found(harness: Harness):
    harness.write_papers([])
    item = inbox.add_pdf(harness.ctx, "x.pdf", PDF, identifier="arXiv:1907.10485")
    assert item.status == "done"
    assert item.error and "1907.10485" in item.error and "查不到" in item.error


def test_upload_pdf_without_identifier_uses_file_name(harness: Harness):
    harness.write_papers([])
    item = inbox.add_pdf(harness.ctx, "My Draft Paper.pdf", PDF, identifier=None)
    assert item.status == "done" and item.paper_id
    assert harness.ctx.store.papers.get(item.paper_id).title == "My Draft Paper"
    assert item.error and "元数据待补" in item.error
    harness.run()
    assert harness.ctx.store.papers.get_fulltext(item.paper_id).status == "parsed"


def test_upload_same_pdf_twice_reuses_the_paper(harness: Harness):
    harness.write_papers([])
    first = inbox.add_pdf(harness.ctx, "short.pdf", PDF, identifier=None)
    second = inbox.add_pdf(harness.ctx, "renamed.pdf", PDF, identifier=None)
    assert first.paper_id == second.paper_id


@pytest.mark.parametrize("content", [b"not a pdf", b""])
def test_upload_rejects_non_pdf(harness: Harness, content: bytes):
    with pytest.raises(InvalidRequest, match="PDF"):
        inbox.add_pdf(harness.ctx, "x.pdf", content, identifier=None)


def test_upload_rejects_too_large(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(inbox, "MAX_PDF_BYTES", 10)
    with pytest.raises(InvalidRequest, match="超过"):
        inbox.add_pdf(harness.ctx, "x.pdf", PDF, identifier=None)


# ---- HTTP 接口 ----

BASE = "http://127.0.0.1:48125"


@pytest.fixture
def client(harness_with_offtopic: Harness) -> TestClient:
    app = create_app(harness_with_offtopic.ctx, LocalSecurity(48125, launch_token="t"), None)
    test_client = TestClient(app, base_url=BASE)
    test_client.get("/launch", params={"token": "t"}, follow_redirects=False)
    test_client.headers["X-CSRF-Token"] = test_client.get("/api/session").json()["csrf_token"]
    return test_client


def test_http_add_paper_by_doi(client: TestClient):
    response = client.post("/api/inbox/papers", json={"text": "10.1000/off-topic-paper"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "done" and body["paper_id"]
    assert client.post("/api/inbox/papers", json={"text": "hello"}).status_code == 422


def test_http_upload_pdf(client: TestClient):
    headers = {"Content-Type": "application/pdf", "X-File-Name": "My%20Paper.pdf"}
    response = client.post("/api/inbox/files", content=PDF, headers=headers)
    assert response.status_code == 200
    assert response.json()["paper_id"]
    wrong = client.post("/api/inbox/files", content=b"x", headers={"Content-Type": "text/plain"})
    assert wrong.status_code == 422


def test_http_upload_size_is_limited_but_larger_than_normal_requests(client: TestClient):
    big = b"%PDF-1.7\n" + b"0" * (200 * 1024)  # 超过普通请求的 64 KB，仍在上传上限内
    headers = {"Content-Type": "application/pdf", "X-File-Name": "big.pdf"}
    assert client.post("/api/inbox/files", content=big, headers=headers).status_code == 200
    assert client.post("/api/annotations/x", content=big).status_code == 413
