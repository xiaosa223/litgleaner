"""全局检索的实际内容、来源、位置和即时更新；外部服务不参与。"""

import httpx
import pytest
from fastapi.testclient import TestClient

from litgleaner.application import reader, search
from litgleaner.application.codec import compress_json
from litgleaner.application.documents import save_document
from litgleaner.application.stages.discover import resolve_record
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.reading import NewAnnotation
from litgleaner.ports.store import FullTextState
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness


def _paper(h: Harness, title: str = "Neptune Study", **fields) -> str:
    return resolve_record(h.ctx.store.papers, SourceRecord(title=title, **fields), h.ctx.now_iso())


def _archived(h: Harness, paper_id: str, blocks: tuple[Block, ...], version: str = "v1") -> str:
    document = NormalizedDocument(1, "a" * 64, version, None, blocks, (), "ok")
    key, _ = save_document(h.ctx, document)
    h.ctx.store.papers.save_fulltext(
        FullTextState(paper_id, "parsed", doc_key=key, quality="ok"), h.ctx.now_iso()
    )
    return key


def test_searches_metadata_authors_venue_doi_and_abstract_without_library_membership(
    harness: Harness,
):
    paper_id = _paper(harness, "Molecular Docking", authors=("Alice Zephyr",),
                     venue="Journal of Biophysics", doi="10.1000/docking-study",
                     abstract="The hidden receptor regulates binding.")  # fmt: skip
    for query in ("MOLECULAR", "Zephyr", "Biophysics", "10.1000/docking-study", "hidden receptor"):
        hits = search.search(harness.ctx, query).hits
        assert len(hits) == 1
        assert (hits[0].paper_id, hits[0].kind, hits[0].authorship) == (paper_id, "paper", "author")
        assert hits[0].title == "Molecular Docking" and hits[0].page is None
    assert harness.ctx.store.reader.get_state(paper_id) is None
    assert harness.llm_calls() == 0


def test_identifier_added_after_paper_is_searchable_immediately(harness: Harness):
    paper_id = _paper(harness)
    assert search.search(harness.ctx, "late-alias").hits == ()
    harness.ctx.store.papers.add_aliases(paper_id, (Alias("doi", "10.1234/late-alias"),))
    assert search.search(harness.ctx, "late-alias").hits[0].paper_id == paper_id


def test_archived_fulltext_returns_exact_block_and_zero_based_page(harness: Harness):
    paper_id = _paper(harness)
    _archived(
        harness,
        paper_id,
        (
            Block("heading", "Experiment", 0),
            Block("text", "A distant quasar emits coherent radiation.", 7),
        ),
    )
    hits = search.search(harness.ctx, "coherent radiation").hits
    assert len(hits) == 1
    hit = hits[0]
    assert (hit.kind, hit.id, hit.paper_id, hit.page, hit.authorship) == (
        "fulltext",
        "1",
        paper_id,
        7,
        "author",
    )
    assert "coherent radiation" in hit.text


@pytest.mark.parametrize("query", ["神经", "神经网", "卷积 神经"])
def test_searches_chinese_short_and_trigram_terms(harness: Harness, query: str):
    paper_id = _paper(harness, "卷积神经网络的分析")
    assert [h.paper_id for h in search.search(harness.ctx, query).hits] == [paper_id]


@pytest.mark.parametrize(
    "query", ["' OR 1=1 --", '"Neptune" OR "missing"', "(missing):{}", "!!!", "%"]
)
def test_sql_and_fts_syntax_cannot_escape_literal_term_search(harness: Harness, query: str):
    _paper(harness)
    assert search.search(harness.ctx, query).hits == ()


def test_underscore_is_a_literal_character_for_short_term_matching(harness: Harness):
    matching = _paper(harness, "x_y")
    _paper(harness, "xay")
    assert [h.paper_id for h in search.search(harness.ctx, "_").hits] == [matching]


def test_notes_and_highlights_preserve_author_quote_and_user_body_sources(harness: Harness):
    paper_id = _paper(harness)
    annotation = reader.add_annotation(
        harness.ctx,
        NewAnnotation(paper_id, "note", 3, None, "The blue nebula is visible.", "我的望远镜标记"),
    )
    quote = search.search(harness.ctx, "blue nebula").hits[0]
    body = search.search(harness.ctx, "望远镜").hits[0]
    assert (quote.id, quote.kind, quote.authorship, quote.page) == (
        annotation.id + ":quote",
        "highlight",
        "author",
        3,
    )
    assert (body.id, body.kind, body.authorship, body.page) == (
        annotation.id + ":body",
        "note",
        "user",
        3,
    )
    reader.edit_annotation(harness.ctx, annotation.id, "改成星云观测")
    assert search.search(harness.ctx, "望远镜").hits == ()
    assert search.search(harness.ctx, "星云").hits[0].authorship == "user"
    assert search.search(harness.ctx, "blue nebula").hits[0].authorship == "author"
    reader.remove_annotation(harness.ctx, annotation.id)
    assert search.search(harness.ctx, "星云").hits == ()
    assert search.search(harness.ctx, "blue nebula").hits == ()


def test_extraction_search_is_marked_model_and_only_latest_version_is_visible(harness: Harness):
    paper_id = _paper(harness)
    repo = harness.ctx.store.papers
    repo.insert_extraction(
        paper_id, "hash1", "v1", {"summary": "An obsolete hypothesis"}, harness.ctx.now_iso()
    )
    assert search.search(harness.ctx, "obsolete").hits[0].authorship == "model"
    repo.insert_extraction(
        paper_id,
        "hash2",
        "v2",
        {
            "summary": "The revised hypothesis",
            "problem": "Identifying magnetars",
            "approach": "Using spectroscopy",
        },
        harness.ctx.now_iso(),
    )
    assert search.search(harness.ctx, "obsolete").hits == ()
    for query in ("revised", "magnetars", "spectroscopy"):
        hit = search.search(harness.ctx, query).hits[0]
        assert (hit.kind, hit.authorship, hit.paper_id) == ("extraction", "model", paper_id)


def test_old_fulltext_is_indexed_once_then_blob_reads_stop(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    paper_id = _paper(harness)
    key = _archived(harness, paper_id, (Block("text", "Cached astronomy finding", 2),))
    reads: list[str] = []
    get = harness.ctx.blobs.get

    def counted(blob_key: str) -> bytes:
        reads.append(blob_key)
        return get(blob_key)

    monkeypatch.setattr(harness.ctx.blobs, "get", counted)
    assert search.search(harness.ctx, "astronomy").hits[0].page == 2
    assert reads == [key]
    assert search.search(harness.ctx, "finding").hits[0].paper_id == paper_id
    assert reads == [key]


def test_empty_document_is_still_marked_indexed(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    paper_id = _paper(harness)
    _archived(harness, paper_id, ())
    assert search.search(harness.ctx, "missing").hits == ()

    def no_read(_key: str) -> bytes:
        pytest.fail("已经索引过的空文档不应再次读归档")

    monkeypatch.setattr(harness.ctx.blobs, "get", no_read)
    assert search.search(harness.ctx, "missing").hits == ()


def test_new_document_version_replaces_old_text_and_position(harness: Harness):
    paper_id = _paper(harness)
    _archived(harness, paper_id, (Block("text", "Old supernova result", 1),))
    assert search.search(harness.ctx, "supernova").hits[0].page == 1
    _archived(harness, paper_id, (Block("text", "New pulsar result", 8),), version="v2")
    assert search.search(harness.ctx, "supernova").hits == ()
    assert search.search(harness.ctx, "pulsar").hits[0].page == 8


def test_search_does_not_use_network_or_models(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    _paper(harness, "Local Astronomy")

    def denied(*_args, **_kwargs):
        pytest.fail("本地检索不应调用网络或模型")

    monkeypatch.setattr(httpx.Client, "send", denied)
    monkeypatch.setattr(type(harness.ctx.llm), "call", denied)
    monkeypatch.setattr(type(harness.ctx.llm), "stream", denied)
    assert search.search(harness.ctx, "astronomy").hits
    assert harness.llm_calls() == 0


def test_empty_query_skips_index_backfill(harness: Harness, monkeypatch: pytest.MonkeyPatch):
    paper_id = _paper(harness)
    _archived(harness, paper_id, (Block("text", "Not needed yet", 0),))

    def no_read(_key: str) -> bytes:
        pytest.fail("空查询不应读取归档")

    monkeypatch.setattr(harness.ctx.blobs, "get", no_read)
    assert search.search(harness.ctx, " \n\t ").hits == ()


@pytest.mark.parametrize(
    "corrupted", [b"not zstd", compress_json({}), compress_json({"blocks": 3})]
)
def test_corrupted_archive_does_not_hide_valid_search_results(harness: Harness, corrupted: bytes):
    paper_id = _paper(harness, "Reliable Astronomy")
    key = _archived(harness, paper_id, (Block("text", "Broken archive", 1),))
    harness.ctx.blobs.put(key, corrupted)
    assert search.search(harness.ctx, "Reliable").hits[0].paper_id == paper_id


@pytest.mark.parametrize("query,limit", [("x" * 501, 30), ("x", 0), ("x", 101)])
def test_search_validates_limits(harness: Harness, query: str, limit: int):
    with pytest.raises(InvalidRequest):
        search.search(harness.ctx, query, limit)


def test_http_search_session_limits_and_empty_query(harness: Harness):
    for index in range(4):
        _paper(harness, f"Astronomy Study {index}")
    client = TestClient(create_app(harness.ctx, LocalSecurity(48127, launch_token="t"), None),
                        base_url="http://127.0.0.1:48127")  # fmt: skip
    assert client.get("/api/search", params={"q": "astronomy"}).status_code == 401
    client.get("/launch", params={"token": "t"}, follow_redirects=False)
    assert client.get("/api/search").json() == {"hits": []}
    response = client.get("/api/search", params={"q": "astronomy", "limit": 2})
    assert response.status_code == 200 and len(response.json()["hits"]) == 2
    for params in ({"q": "x" * 501}, {"limit": 0}, {"limit": 101}, {"limit": "bad"}):
        assert client.get("/api/search", params=params).status_code == 422
