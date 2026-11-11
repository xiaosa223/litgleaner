"""完整文库：用户范围、状态、课题、文字筛选与稳定分页。"""

from dataclasses import asdict

import pytest
from fastapi.testclient import TestClient

from litgleaner.application import catalog, reader
from litgleaner.application.stages.discover import resolve_record
from litgleaner.domain.digest import PaperCard
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.inbox import InboxItem
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.reading import NewAnnotation
from litgleaner.ports.catalog import CatalogQuery
from litgleaner.ports.store import DigestItem, DigestRecord, TopicState, TriageResult
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness


def _add(h: Harness, title: str, **fields) -> str:
    return resolve_record(h.ctx.store.papers, SourceRecord(title=title, **fields), h.ctx.now_iso())


def _note(h: Harness, paper_id: str):
    return reader.add_annotation(h.ctx, NewAnnotation(paper_id, "note", 2, None, None, "笔记"))


def _triage(h: Harness, paper_id: str, topic_id: str, topic_hash: str, relevant: bool) -> None:
    h.ctx.store.judgments.save_triage(
        TriageResult(paper_id, topic_id, topic_hash, relevant, "reason", "v1", "test"),
        h.ctx.now_iso(),
    )


def _push(h: Harness, paper_id: str, topic_id: str, digest_id: str) -> None:
    card = PaperCard(
        paper_id, "Old cached title", ("Old author",), None, None, None, 95, "must_read", False,
        "A summary", None, (), None, None, (), True, "ok", "必读",
    )  # fmt: skip
    digest = DigestRecord(digest_id, digest_id, topic_id, "me", "2026-01-01")
    h.ctx.store.outputs.create_digest(
        digest, (DigestItem(paper_id, False, asdict(card)),), h.ctx.now_iso()
    )


def test_library_keeps_read_and_abandoned_and_excludes_untouched_candidates(harness: Harness):
    read_id = _add(harness, "Read Long Ago")
    abandoned_id = _add(harness, "Abandoned Long Ago")
    _add(harness, "Untouched Candidate")
    reader.set_reading_state(harness.ctx, read_id, "read", None)
    reader.set_reading_state(harness.ctx, abandoned_id, "abandoned", None)

    page = catalog.library(harness.ctx)

    assert {item.paper_id for item in page.items} == {read_id, abandoned_id}
    assert page.total == 2
    assert page.counts == {"all": 2, "new": 0, "queued": 0, "reading": 0, "read": 1, "abandoned": 1}
    assert catalog.library(harness.ctx, CatalogQuery(status="read")).total == 1
    assert (
        catalog.library(harness.ctx, CatalogQuery(status="abandoned")).items[0].paper_id
        == abandoned_id
    )
    assert harness.llm_calls() == 0


def test_only_live_annotations_and_finished_user_additions_enter_library(harness: Harness):
    note_id = _add(harness, "Only A Note")
    deleted_id = _add(harness, "Deleted Note")
    uploaded_id = _add(harness, "Uploaded File")
    pending_id = _add(harness, "Pending Add")
    _note(harness, note_id)
    old_note = _note(harness, deleted_id)
    reader.remove_annotation(harness.ctx, old_note.id)
    now = harness.ctx.now_iso()
    harness.ctx.store.inbox.add(InboxItem("i1", "pdf", "x.pdf", "done", now, paper_id=uploaded_id))
    harness.ctx.store.inbox.add(InboxItem("i2", "doi", "x", "pending", now, paper_id=pending_id))

    page = catalog.library(harness.ctx, CatalogQuery(status="new"))

    assert {item.paper_id for item in page.items} == {note_id, uploaded_id}
    assert page.counts["new"] == 2
    assert all(item.reading_status is None for item in page.items)


def test_topic_filter_matches_each_current_relevant_topic_and_historical_push(harness: Harness):
    paper_id = _add(harness, "Two Topics")
    reader.set_reading_state(harness.ctx, paper_id, "reading", None)
    alpha = harness.ctx.topics["alpha-topic"]
    _triage(harness, paper_id, alpha.id, alpha.topic_hash, True)
    now = harness.ctx.now_iso()
    harness.ctx.store.judgments.save_topic_state(TopicState("beta", "active", "b2", now), now)
    _triage(harness, paper_id, "beta", "b2", True)
    _push(harness, paper_id, "historical", "digest-old")

    for topic_id in (alpha.id, "beta", "historical"):
        page = catalog.library(harness.ctx, CatalogQuery(topic_id=topic_id))
        assert page.total == 1
        assert page.items[0].topic_id == topic_id
        assert page.items[0].topic_ids == ("alpha-topic", "beta", "historical")
        assert page.items[0].title == "Two Topics"  # 当前元数据优先于旧卡片
    historic = catalog.library(harness.ctx, CatalogQuery(topic_id="historical")).items[0]
    assert historic.score == 95 and historic.tier_label == "必读"
    assert historic.pushed_at == now


def test_false_and_outdated_triage_do_not_create_topic_relationships(harness: Harness):
    paper_id = _add(harness, "Unrelated Paper")
    _note(harness, paper_id)
    alpha = harness.ctx.topics["alpha-topic"]
    _triage(harness, paper_id, alpha.id, "old-hash", True)
    _triage(harness, paper_id, alpha.id, alpha.topic_hash, False)
    assert catalog.library(harness.ctx, CatalogQuery(topic_id=alpha.id)).total == 0
    assert catalog.library(harness.ctx).items[0].topic_ids == ()


def test_counts_ignore_status_and_page_but_respect_topic_and_text(harness: Harness):
    alpha = harness.ctx.topics["alpha-topic"]
    for title, status in (("Alpha One", "read"), ("Alpha Two", "read"), ("Alpha Three", "queued")):
        paper_id = _add(harness, title)
        harness.ctx.store.reader.set_state(paper_id, status, None, harness.ctx.now_iso())
        _triage(harness, paper_id, alpha.id, alpha.topic_hash, True)
    other = _add(harness, "Other Paper")
    reader.set_reading_state(harness.ctx, other, "read", None)

    query = CatalogQuery(status="read", topic_id=alpha.id, q="alpha", limit=1, offset=1)
    page = catalog.library(harness.ctx, query)
    assert len(page.items) == 1 and page.total == 2
    assert page.counts["all"] == 3 and page.counts["queued"] == 1 and page.counts["read"] == 2


def test_metadata_search_is_literal_and_matches_all_words_across_fields(harness: Harness):
    paper_id = _add(harness, "100%_Efficient Training", authors=("Alice Smith",),
                    venue="Research Venue", doi="10.1000/literal-doi")  # fmt: skip
    _note(harness, paper_id)
    other = _add(harness, "100XCoverage")
    _note(harness, other)
    for query in ("%_", "Alice efficient", "RESEARCH", "10.1000/literal-doi"):
        assert [p.paper_id for p in catalog.library(harness.ctx, CatalogQuery(q=query)).items] == [
            paper_id
        ]
    assert catalog.library(harness.ctx, CatalogQuery(q="' OR 1=1 --")).total == 0
    assert catalog.library(harness.ctx, CatalogQuery(q="Alice missing")).total == 0


def test_pagination_is_stable_and_not_limited_to_recent_recommendations(harness: Harness):
    ids = []
    with harness.ctx.store.atomic():
        for index in range(205):
            paper_id = _add(harness, f"Old Read Paper {index}")
            reader.set_reading_state(harness.ctx, paper_id, "read", None)
            ids.append(paper_id)
    first = catalog.library(harness.ctx, CatalogQuery(limit=200))
    last = catalog.library(harness.ctx, CatalogQuery(limit=200, offset=200))
    assert first.total == last.total == 205
    assert [p.paper_id for p in (*first.items, *last.items)] == sorted(ids)
    assert catalog.library(harness.ctx, CatalogQuery(offset=205)).items == ()
    assert harness.llm_calls() == 0


@pytest.mark.parametrize("query", [CatalogQuery(limit=0), CatalogQuery(limit=201),
                                  CatalogQuery(offset=-1), CatalogQuery(q="x" * 501)])  # fmt: skip
def test_library_rejects_out_of_range_queries(harness: Harness, query: CatalogQuery):
    with pytest.raises(InvalidRequest):
        catalog.library(harness.ctx, query)


def test_http_library_requires_session_and_validates_filters(harness: Harness):
    paper_id = _add(harness, "Stored Paper")
    reader.set_reading_state(harness.ctx, paper_id, "read", None)
    client = TestClient(create_app(harness.ctx, LocalSecurity(48126, launch_token="t"), None),
                        base_url="http://127.0.0.1:48126")  # fmt: skip
    assert client.get("/api/library").status_code == 401
    client.get("/launch", params={"token": "t"}, follow_redirects=False)
    response = client.get("/api/library", params={"status": "read", "q": "Stored"})
    assert response.status_code == 200
    assert response.json()["items"][0]["paper_id"] == paper_id
    for params in ({"status": "bad"}, {"limit": 201}, {"offset": -1}, {"q": "x" * 501}):
        assert client.get("/api/library", params=params).status_code == 422
