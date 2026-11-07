"""本地 HTTP 接口：第 15.5 节的本地安全要求和各接口的协议适配。"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, paper

PORT = 48123
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def security() -> LocalSecurity:
    return LocalSecurity(port=PORT, launch_token="launch-secret")


@pytest.fixture
def client(harness: Harness, security: LocalSecurity) -> TestClient:
    harness.write_papers([paper("Top Paper", 5)])
    harness.run()
    return TestClient(create_app(harness.ctx, security, static_dir=None), base_url=BASE)


def _login(client: TestClient) -> str:
    response = client.get("/launch", params={"token": "launch-secret"}, follow_redirects=False)
    assert response.status_code == 303
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()
    token = client.get("/api/session").json()["csrf_token"]
    assert token
    return str(token)


def _paper_id(client: TestClient) -> str:
    return str(client.get("/api/inbox").json()["recommended"][0]["paper_id"])


def test_api_requires_session_from_launch_link(client: TestClient):
    assert client.get("/api/inbox").status_code == 401
    wrong = client.get("/launch", params={"token": "guess"}, follow_redirects=False)
    assert wrong.status_code == 403
    _login(client)
    assert client.get("/api/inbox").status_code == 200


def test_rejects_foreign_host_header_against_dns_rebinding(client: TestClient):
    _login(client)
    response = client.get("/api/inbox", headers={"Host": "evil.example:48123"})
    assert response.status_code == 400
    assert client.get("/api/inbox", headers={"Host": f"localhost:{PORT}"}).status_code == 200


def test_rejects_cross_site_origin(client: TestClient):
    _login(client)
    response = client.get("/api/inbox", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_state_change_needs_csrf_token(client: TestClient):
    csrf = _login(client)
    paper_id = _paper_id(client)
    url = f"/api/papers/{paper_id}/reading"
    assert client.put(url, json={"status": "queued"}).status_code == 403
    bad = client.put(url, json={"status": "queued"}, headers={"X-CSRF-Token": "nope"})
    assert bad.status_code == 403
    ok = client.put(url, json={"status": "queued"}, headers={"X-CSRF-Token": csrf})
    assert ok.status_code == 204
    assert client.get("/api/inbox").json()["queued"][0]["paper_id"] == paper_id


def test_security_headers_are_set(client: TestClient):
    _login(client)
    headers = client.get("/api/inbox").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["cache-control"] == "no-store"


def test_paper_document_and_pdf(client: TestClient):
    _login(client)
    paper_id = _paper_id(client)
    view = client.get(f"/api/papers/{paper_id}").json()
    assert view["title"] == "Top Paper"
    assert view["assessments"]["alpha-topic"]["tier"] == "must_read"
    blocks = client.get(f"/api/papers/{paper_id}/document").json()["blocks"]
    assert blocks[0]["index"] == 0
    pdf = client.get(f"/api/papers/{paper_id}/pdf")
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF")
    assert client.get("/api/papers/missing").status_code == 404


def test_annotation_lifecycle_and_validation(client: TestClient):
    csrf = {"X-CSRF-Token": _login(client)}
    paper_id = _paper_id(client)
    url = f"/api/papers/{paper_id}/annotations"
    bad = client.post(url, json={"kind": "script", "body": "x"}, headers=csrf)
    assert bad.status_code == 422
    too_long = client.post(url, json={"kind": "note", "body": "x" * 20001}, headers=csrf)
    assert too_long.status_code == 422

    created = client.post(
        url,
        json={
            "kind": "highlight",
            "page": 0,
            "bbox": [1, 2, 3, 4],
            "rects": [[10, 20, 300, 12]],
            "quote": "We study",
        },
        headers=csrf,
    )
    assert created.json()["rects"] == [[10, 20, 300, 12]]
    assert created.status_code == 201
    note_id = created.json()["id"]
    edit = client.patch(f"/api/annotations/{note_id}", json={"body": "关键句"}, headers=csrf)
    assert edit.status_code == 204
    annotations = client.get(f"/api/papers/{paper_id}").json()["annotations"]
    assert [(a["quote"], a["body"]) for a in annotations] == [("We study", "关键句")]
    assert client.delete(f"/api/annotations/{note_id}", headers=csrf).status_code == 204
    assert client.delete(f"/api/annotations/{note_id}", headers=csrf).status_code == 404


def test_serves_built_frontend_with_spa_fallback(
    harness: Harness, security: LocalSecurity, tmp_path: Path
):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>拾穗</title>", encoding="utf-8")
    (static / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    client = TestClient(create_app(harness.ctx, security, static_dir=static), base_url=BASE)

    assert client.get("/").status_code == 401  # 页面同样需要会话
    _login(client)
    assert "拾穗" in client.get("/").text
    assert "拾穗" in client.get("/papers/abc").text
    assert client.get("/assets/app.js").text == "console.log(1)"
    assert client.get("/api/unknown").status_code == 404


@pytest.mark.parametrize(
    "position",
    [
        {f"k{i}": i for i in range(9)},
        {"view": "x" * 65},
        {"y" * 65: 1},
    ],
)
def test_reading_position_is_bounded(client: TestClient, position: dict):
    csrf = {"X-CSRF-Token": _login(client)}
    url = f"/api/papers/{_paper_id(client)}/reading"
    body = {"status": "reading", "position": position}
    assert client.put(url, json=body, headers=csrf).status_code == 422


def test_sessions_are_capped_and_expire():
    clock = [1000.0]
    security = LocalSecurity(port=PORT, launch_token="t", max_sessions=2, idle_seconds=60,
                             now=lambda: clock[0])  # fmt: skip
    first, second, third = (security.new_session() for _ in range(3))
    assert security.csrf_token(first) is None  # 超过上限时淘汰最久没用的
    assert security.csrf_token(second) and security.csrf_token(third)
    clock[0] += 61
    assert security.csrf_token(third) is None


def test_request_body_size_is_limited(client: TestClient):
    csrf = {"X-CSRF-Token": _login(client)}
    url = f"/api/papers/{_paper_id(client)}/annotations"
    big = {"kind": "note", "body": "x" * 70_000}
    assert client.post(url, json=big, headers=csrf).status_code == 413


def test_bbox_and_position_numbers_are_bounded(client: TestClient):
    csrf = {"X-CSRF-Token": _login(client)}
    paper_id = _paper_id(client)
    bbox = {"kind": "highlight", "bbox": [0, 0, 10**12, 1], "quote": "q"}
    assert (
        client.post(f"/api/papers/{paper_id}/annotations", json=bbox, headers=csrf).status_code
        == 422
    )
    body = {"status": "reading", "position": {"page": 10**12}}
    assert client.put(f"/api/papers/{paper_id}/reading", json=body, headers=csrf).status_code == 422


def test_pdf_is_sandboxed_when_opened_directly(client: TestClient):
    _login(client)
    pdf = client.get(f"/api/papers/{_paper_id(client)}/pdf")
    assert pdf.headers["content-security-policy"].startswith("sandbox")


def test_highlight_rects_are_bounded(client: TestClient):
    csrf = {"X-CSRF-Token": _login(client)}
    url = f"/api/papers/{_paper_id(client)}/annotations"
    too_many = {"kind": "highlight", "rects": [[0, 0, 1, 1]] * 201}
    assert client.post(url, json=too_many, headers=csrf).status_code == 422
    outside = {"kind": "highlight", "rects": [[0, 0, 1001, 1]]}
    assert client.post(url, json=outside, headers=csrf).status_code == 422


def test_costs_endpoint(client: TestClient):
    _login(client)
    assert client.get("/api/costs").json()["month_usd"] >= 0


def _events(response) -> list[tuple[str, dict]]:
    events, name = [], None
    for line in response.iter_lines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: "):
            events.append((name, json.loads(line[6:])))
    return events


def test_translate_streams_deltas_then_result_and_is_listed(client: TestClient):
    csrf = {"X-CSRF-Token": _login(client)}
    paper_id = _paper_id(client)
    url = f"/api/papers/{paper_id}/blocks/2/translate"
    assert client.post(url).status_code == 403  # 状态变更：要 CSRF 令牌
    with client.stream("POST", url, headers=csrf) as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _events(response)
    names = [name for name, _ in events]
    assert names.count("delta") >= 2 and names[-1] == "done"
    done = events[-1][1]
    assert done["verified"] and done["sentences"] == ["【译】We study the problem."]

    listed = client.get(f"/api/papers/{paper_id}/translations").json()
    assert [item["index"] for item in listed] == [2]
    costs = client.get("/api/costs", params={"paper_id": paper_id}).json()
    assert costs["paper_usd"] == pytest.approx(0.001)


def test_translate_errors_arrive_as_error_event(client: TestClient):
    csrf = {"X-CSRF-Token": _login(client)}
    url = f"/api/papers/{_paper_id(client)}/blocks/1/translate"
    with client.stream("POST", url, headers=csrf) as response:
        events = _events(response)
    assert events == [("error", {"message": "这一块不需要翻译（标题、公式、表格或中文段落）"})]
