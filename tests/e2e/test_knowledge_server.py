"""知识库事实接口贯通提取、原页证据、来源检索和旧版本只读。"""

import pytest
from fastapi.testclient import TestClient

from litgleaner.ports.llm import LLMCall
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, extraction_answer, paper


def _client(harness: Harness) -> TestClient:
    client = TestClient(
        create_app(harness.ctx, LocalSecurity(48132, launch_token="knowledge-test"), None),
        base_url="http://127.0.0.1:48132",
    )
    client.get("/launch", params={"token": "knowledge-test"})
    return client


@pytest.fixture
def knowledge_ui(harness: Harness):
    harness.write_papers([paper("Knowledge One", 5), paper("Knowledge Two", 5)])
    harness.llm.overrides["extract"] = {
        **extraction_answer(),
        "summary": "概念事实样例",
        "conclusion": "方法有效",
        "innovation": None,
        "limitations": [],
        "key_concepts": [
            {
                "name": "problem",
                "role": "core",
                "definition": "We study the problem.",
                "page": 0,
            },
            {
                "name": "Study",
                "role": "prerequisite",
                "definition": "fabricated definition with no source",
                "page": 0,
            },
            {"name": "works", "role": "mentioned", "definition": None, "page": 1},
        ],
    }
    assert harness.run().ok
    with _client(harness) as client:
        yield harness, client


def test_knowledge_endpoints_share_global_facts_with_real_paper_positions(knowledge_ui):
    harness, client = knowledge_ui
    before = harness.llm_calls()
    page = client.get("/api/knowledge/concepts").json()
    assert page["total"] == 3 and len(page["items"]) == 3
    item = next(i for i in page["items"] if i["concept"]["name"] == "problem")
    assert item["papers"] == 2
    assert "level" not in item and "due" not in item
    assert item["definition"]["quote"] == "We study the problem."
    assert item["definition"]["page"] == 0
    assert item["definition"]["authorship"] == "author"
    detail = client.get(f"/api/knowledge/concepts/{item['concept']['id']}").json()
    assert len(detail["occurrences"]) == 2
    assert all(not o["stale"] for o in detail["occurrences"])
    assert all(o["definition_authorship"] == "author" for o in detail["occurrences"])
    paper_id = detail["occurrences"][0]["occurrence"]["paper_id"]
    found = client.get(f"/api/papers/{paper_id}/concepts").json()
    assert len(found) == 3
    assert all(o["occurrence"]["paper_title"].startswith("Knowledge") for o in found)
    assert harness.llm_calls() == before


def test_unverified_definitions_do_not_enter_facts_or_author_search(knowledge_ui):
    _, client = knowledge_ui
    page = client.get("/api/knowledge/concepts", params={"q": "Study"}).json()
    item = next(i for i in page["items"] if i["concept"]["name"] == "Study")
    assert item["definition"] is None
    detail = client.get(f"/api/knowledge/concepts/{item['concept']['id']}").json()
    assert all(o["occurrence"]["definition"] is None for o in detail["occurrences"])
    assert all(o["definition_authorship"] is None for o in detail["occurrences"])
    assert not client.get("/api/search", params={"q": "fabricated definition"}).json()["hits"]
    hits = client.get("/api/search", params={"q": "problem"}).json()["hits"]
    concepts = [hit for hit in hits if hit["kind"] == "concept"]
    assert len(concepts) == 2
    assert all(hit["authorship"] == "author" and hit["page"] == 0 for hit in concepts)
    assert all(hit["kind"] != "concept_name" for hit in hits)


def test_knowledge_filter_count_and_stable_pagination(knowledge_ui):
    _, client = knowledge_ui
    path = "/api/knowledge/concepts"
    ids = []
    for offset in range(3):
        response = client.get(
            path, params={"topic_id": "alpha-topic", "limit": 1, "offset": offset}
        )
        result = response.json()
        assert result["total"] == 3 and len(result["items"]) == 1
        ids.append(result["items"][0]["concept"]["id"])
    assert len(set(ids)) == 3
    assert client.get(path, params={"topic_id": "unknown"}).json()["total"] == 0
    assert client.get(path, params={"q": '" OR * ( injection'}).status_code == 200
    assert client.get(path, params={"q": "works"}).json()["total"] == 1


def test_language_change_only_marks_concepts_stale_and_preserves_history(knowledge_ui):
    harness, _ = knowledge_ui
    before = harness.llm_calls()
    harness.edit_yaml("app.yaml", lambda d: d["language"].update(reading="en"))
    harness.rebuild()
    with _client(harness) as client:
        page = client.get("/api/knowledge/concepts").json()
        assert page["total"] == 3
        item = next(i for i in page["items"] if i["concept"]["name"] == "problem")
        assert item["definition"]["stale"]
        detail = client.get(f"/api/knowledge/concepts/{item['concept']['id']}").json()
        assert all(o["stale"] for o in detail["occurrences"])
        assert harness.run("extract").ok
    assert harness.llm_calls() == before


def test_budget_and_missing_model_do_not_prevent_reading_concept_facts(knowledge_ui):
    harness, _ = knowledge_ui
    before = harness.llm_calls()
    harness.edit_yaml("llm.yaml", lambda d: d["tasks"].pop("extract"))
    harness.rebuild()
    harness.ctx.store.runs.record_llm_call(
        LLMCall("configuration_test", "test-model", "test", 1, 1, 10, True),
        harness.ctx.now_iso(),
    )
    with _client(harness) as client:
        assert client.get("/api/knowledge/concepts").json()["total"] == 3
    assert harness.llm_calls() == before + 1


def test_knowledge_read_routes_validate_input_and_require_local_session(knowledge_ui):
    harness, client = knowledge_ui
    assert client.get("/api/knowledge/concepts/missing").status_code == 404
    assert client.get("/api/papers/missing/concepts").status_code == 404
    path = "/api/knowledge/concepts"
    for params in ({"limit": 0}, {"offset": -1}, {"q": "x" * 501}):
        assert client.get(path, params=params).status_code == 422
    with TestClient(
        create_app(harness.ctx, LocalSecurity(48132), None),
        base_url="http://127.0.0.1:48132",
    ) as outsider:
        assert outsider.get(path).status_code == 401
