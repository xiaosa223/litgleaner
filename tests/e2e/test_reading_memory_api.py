"""本地阅读摘要的完整JSON、分页、安全与零副作用合同。"""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from litgleaner.domain.identity import Alias
from litgleaner.domain.reading import NewAnnotation
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.memory_helpers import add, citation
from tests.e2e.relate_flow_helpers import generate, respond
from tests.e2e.relate_helpers import HERE, THERE, pair

PORT = 48159


@pytest.fixture
def ui(harness):
    app = create_app(
        harness.ctx,
        LocalSecurity(PORT, launch_token="reading-memory-test"),
        None,
        context_provider=lambda: harness.ctx,
    )
    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}") as client:
        yield client


def login(client):
    response = client.get(
        "/launch", params={"token": "reading-memory-test"}, follow_redirects=False
    )
    assert response.status_code == 303


def test_authenticated_paging_preserves_both_directions_and_all_sources(harness, ui):
    a = add(harness, "anchor")
    others = sorted(add(harness, f"peer-{n}") for n in range(55))
    for other in others:
        citation(harness, a, target=other, position=other)
    second = citation(harness, a, target=others[0], source="remote", position="second")
    back = citation(harness, others[0], target=a)
    base = f"/api/papers/{a}/reading-memory"
    assert ui.get(base).status_code == 401
    login(ui)
    response = ui.get(base)
    assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
    assert response.json()["total"] == 55 and len(response.json()["citations"]) == 50
    pages = [ui.get(base, params={"limit": 20, "offset": n}).json() for n in (0, 20, 40)]
    assert all(page["total"] == 55 for page in pages)
    links = [item for page in pages for item in page["citations"]]
    assert [item["paper"]["paper_id"] for item in links] == others
    assert {item["source"] for item in links[0]["cites"]} == {"parsed", second.source}
    assert links[0]["cited_by"][0]["raw_text"] == back.raw_text
    assert ui.get(base, params={"offset": 100}).json()["citations"] == []


def test_get_never_opens_paper_writes_queues_or_calls_external_services(harness, ui, monkeypatch):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    citation(harness, b, target=a)
    before = harness.ctx.store.reader.get_state(a)
    admission = harness.ctx.store.admissions.get(a)
    login(ui)

    def forbidden(*args, **kwargs):
        raise AssertionError("GET attempted a write or external service")

    monkeypatch.setattr(harness.ctx.store._db, "write", forbidden)
    monkeypatch.setattr(type(harness.ctx.llm), "call", forbidden)
    monkeypatch.setattr(type(harness.llm), "complete", forbidden)
    monkeypatch.setattr(type(harness.ctx.blobs), "put", forbidden)
    for adapter in harness.ctx.sources.values():
        monkeypatch.setattr(type(adapter), "fetch", forbidden)
    response = ui.get(f"/api/papers/{a}/reading-memory")
    assert response.status_code == 200
    (card,) = response.json()["relations"]
    assert card["direction"] == "outgoing" and card["paper"]["paper_id"] == b
    assert card["relation"]["evidence_a"]["quote"] == HERE
    assert card["relation"]["evidence_b"]["quote"] == THERE
    assert harness.ctx.store.reader.get_state(a) == before
    assert harness.ctx.store.admissions.get(a) == admission


def test_missing_model_keeps_citations_and_reports_unknown_relation(harness, ui, monkeypatch):
    a, _ = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    login(ui)
    response = ui.get(f"/api/papers/{a}/reading-memory")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1 and body["relations"] == []
    assert any(item["code"] == "relation_unknown" for item in body["warnings"])


def test_notes_keep_user_authorship_page_and_updated_version_then_disappear(harness, ui):
    a, b = add(harness, "a"), add(harness, "b")
    citation(harness, a, target=b)
    note = harness.ctx.store.reader.add_annotation(
        NewAnnotation(b, "note", 7, None, "Author words", "用户正文\n后续内容"), "t1"
    )
    login(ui)
    path = f"/api/papers/{a}/reading-memory"
    original = ui.get(path).json()["citations"][0]["note"]
    assert original["source_id"] == note.id and original["authorship"] == "user"
    assert original["text"] == "用户正文" and original["page"] == 7 and original["truncated"]
    harness.ctx.store.reader.update_annotation(note.id, "修改正文", "t2")
    changed = ui.get(path).json()["citations"][0]["note"]
    assert changed["text"] == "修改正文" and not changed["truncated"]
    assert changed["source_version"] != original["source_version"]
    harness.ctx.store.reader.delete_annotation(note.id, "t3")
    assert ui.get(path).json()["citations"][0]["note"] is None


def test_global_transitive_alias_conflict_cannot_leak_through_api(harness, ui):
    a, b, bridge = (add(harness, name) for name in ("a", "b", "bridge"))
    outside = add(harness, "outside", library=False)
    x, y = Alias("local", "x"), Alias("local", "y")
    citation(harness, a, aliases=(Alias("doi", "10.1000/b"), x))
    citation(harness, bridge, aliases=(x, y))
    citation(harness, bridge, aliases=(y, Alias("doi", "10.1000/outside")), position="chain")
    login(ui)
    path = f"/api/papers/{a}/reading-memory"
    assert ui.get(path).json()["citations"] == []
    explicit = citation(harness, a, target=b, aliases=(y,), position="explicit")
    links = ui.get(path).json()["citations"]
    assert len(links) == 1 and links[0]["paper"]["paper_id"] == b
    assert [item["position"] for item in links[0]["cites"]] == [explicit.position]
    assert outside not in {item["paper"]["paper_id"] for item in links}


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"limit": "bad"}])
def test_api_validates_paging(harness, ui, params):
    a = add(harness, "anchor")
    login(ui)
    assert ui.get(f"/api/papers/{a}/reading-memory", params=params).status_code == 422


def test_api_requires_trusted_host_origin_and_library_membership(harness, ui):
    a, outside = add(harness, "a"), add(harness, "outside", library=False)
    login(ui)
    path = f"/api/papers/{a}/reading-memory"
    assert ui.get(path, headers={"Origin": "https://other.test"}).status_code == 403
    assert ui.get(path, headers={"Host": "other.test"}).status_code == 400
    for paper_id in (outside, "missing"):
        assert ui.get(f"/api/papers/{paper_id}/reading-memory").status_code == 404
