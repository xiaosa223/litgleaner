"""知识库“笔记”汇总全部论文的笔记和高亮，最近改的在前，删除的不出现。"""

from fastapi.testclient import TestClient

from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, paper


def _client(harness: Harness) -> TestClient:
    client = TestClient(
        create_app(harness.ctx, LocalSecurity(48134, launch_token="notes-test"), None),
        base_url="http://127.0.0.1:48134",
    )
    client.get("/launch", params={"token": "notes-test"})
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return client


def test_notes_across_papers_newest_first_without_deleted(harness: Harness):
    harness.write_papers([paper("Notes One", 5), paper("Notes Two", 5)])
    assert harness.run().ok
    with _client(harness) as client:
        ids = [
            h["paper_id"] for h in client.get("/api/search", params={"q": "Notes"}).json()["hits"]
        ]
        first, second = sorted(set(ids))[:2]
        note = {"kind": "note", "page": 0, "quote": "We study the problem.", "body": "第一条"}
        a = client.post(f"/api/papers/{first}/annotations", json=note).json()
        harness.clock.advance(seconds=5)
        client.post(f"/api/papers/{second}/annotations", json={**note, "body": "第二条"})
        harness.clock.advance(seconds=5)
        b = client.post(f"/api/papers/{first}/annotations", json={**note, "body": "要删的"}).json()
        client.delete(f"/api/annotations/{b['id']}")
        items = client.get("/api/notes").json()
        assert [i["annotation"]["body"] for i in items] == ["第二条", "第一条"]
        assert items[1]["annotation"]["id"] == a["id"]
        assert all(i["paper_title"].startswith("Notes") for i in items)
        assert client.get("/api/notes", params={"limit": 0}).status_code == 422
