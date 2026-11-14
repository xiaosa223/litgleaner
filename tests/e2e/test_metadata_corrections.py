"""用户修正论文元数据（ADR 0011）：显示处覆盖，事实层不变，可撤销，进入个人导出。"""

import pytest
from fastapi.testclient import TestClient

from litgleaner.application.export import mine
from litgleaner.domain.correction import split_authors, title_candidate, validate
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.export import export_name
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, paper


def _client(harness: Harness) -> TestClient:
    client = TestClient(
        create_app(harness.ctx, LocalSecurity(48135, launch_token="meta-test"), None),
        base_url="http://127.0.0.1:48135",
    )
    client.get("/launch", params={"token": "meta-test"})
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return client


@pytest.fixture
def setup(harness: Harness):
    harness.write_papers([paper("Source Title", 5)])
    assert harness.run().ok
    with _client(harness) as client:
        paper_id = client.get("/api/search", params={"q": "Source Title"}).json()["hits"][0][
            "paper_id"
        ]
        client.put(f"/api/papers/{paper_id}/reading", json={"status": "queued"})
        yield harness, client, paper_id


def test_correction_overrides_display_but_not_facts_and_can_be_undone(setup):
    harness, client, paper_id = setup
    view = client.put(
        f"/api/papers/{paper_id}/metadata",
        json={
            "title": "Why Should I Trust You?",
            "authors": "Ribeiro、Singh、Guestrin",
            "published_at": "2016",
        },
    ).json()
    assert view["corrected"]["title"] == "Why Should I Trust You?"
    shown = client.get(f"/api/papers/{paper_id}").json()
    assert shown["title"] == "Why Should I Trust You?"
    assert shown["authors"] == ["Ribeiro", "Singh", "Guestrin"] and shown["published_at"] == "2016"
    queued = client.get("/api/inbox").json()["queued"]
    assert queued[0]["title"] == "Why Should I Trust You?"
    assert harness.ctx.store.papers.get(paper_id).title == "Source Title"  # 事实层不变
    files = {f.name: f.content.decode() for f in mine(harness.ctx) if f.name.endswith(".md")}
    assert "用户修正的元数据" in files[export_name("papers", paper_id)]
    client.put(f"/api/papers/{paper_id}/metadata", json={"title": ""})
    assert client.get(f"/api/papers/{paper_id}").json()["title"] == "Source Title"


def test_invalid_corrections_rejected(setup):
    _, client, paper_id = setup
    assert (
        client.put(f"/api/papers/{paper_id}/metadata", json={"published_at": "去年"}).status_code
        == 422
    )
    assert client.put(f"/api/papers/{paper_id}/metadata", json={"doi": "x"}).status_code == 422
    assert client.put("/api/papers/missing/metadata", json={"title": "x"}).status_code == 404


def test_title_candidate_and_apply_are_pure():
    doc = NormalizedDocument(
        1,
        "sha",
        "p",
        None,
        (
            Block("text", "1", 0),
            Block("heading", "“Why Should I Trust You?” Explaining the Predictions", 0),
            Block("text", "Marco Ribeiro marcotcr@cs.uw.edu", 0),
        ),
        (),
        "ok",
    )
    assert title_candidate(doc).startswith("“Why Should I Trust You?")
    short = NormalizedDocument(1, "sha", "p", None, (Block("text", "short", 0),), (), "ok")
    assert title_candidate(short) is None
    titled = NormalizedDocument(1, "sha", "p", "Parser Title Found", (), (), "ok")
    assert title_candidate(titled) == "Parser Title Found"


def test_section_names_are_not_title_candidates():
    doc = NormalizedDocument(
        1,
        "sha",
        "p",
        None,
        (
            Block("text", "Why Should I Trust You? Explaining Predictions Marco", 0),
            Block("heading", "ABSTRACT", 0),
        ),
        (),
        "ok",
    )
    assert title_candidate(doc).startswith("Why Should I Trust You?")


def test_author_splitting_and_date_validation():
    assert split_authors("Smith, John; Lee, Amy") == ("Smith, John", "Lee, Amy")
    assert split_authors("Ribeiro、Singh, Guestrin") == ("Ribeiro", "Singh, Guestrin")
    assert split_authors("Ribeiro, Singh, Guestrin") == ("Ribeiro", "Singh", "Guestrin")
    assert validate("published_at", "2016-08-09") == "2016-08-09"
    for bad in ("2016-13-01", "2016-02-30", "２０１６"):
        with pytest.raises(ValueError):
            validate("published_at", bad)
