"""问答提交、历史与批注。模型只由按需阶段调用。"""

import pytest
from fastapi.testclient import TestClient

from litgleaner.ports.store import LLMCall
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, paper


@pytest.fixture
def question_ui(harness: Harness):
    harness.write_papers([paper("Question Study", 5), paper("Another Study", 5)])
    assert harness.run().ok
    paper_id = harness.ctx.store.judgments.papers_with_scores()[0]
    harness.edit_yaml(
        "llm.yaml",
        lambda data: data["tasks"].update(
            ask={"provider": "main", "model": "cheap", "prompt": "ask"}
        ),
    )
    harness.rebuild()
    harness.llm.overrides["ask"] = {
        "statements": [
            {
                "text": "模型解释机制",
                "citations": [{"paper_id": paper_id, "page": 1, "quote": "We found it works."}],
            },
            {"text": "没有原文依据的推断", "citations": []},
        ]
    }
    client = TestClient(
        create_app(harness.ctx, LocalSecurity(48131, launch_token="qa-test"), None),
        base_url="http://127.0.0.1:48131",
    )
    client.get("/launch", params={"token": "qa-test"})
    csrf = {"X-CSRF-Token": client.get("/api/session").json()["csrf_token"]}
    yield harness, paper_id, client, csrf
    client.close()


def _submit(client: TestClient, paper_id: str, headers: dict[str, str]):
    response = client.post(
        f"/api/papers/{paper_id}/qa",
        json={"text": "方法为什么有效？", "request_id": "question-1"},
        headers=headers,
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_question_lifecycle_is_queued_idempotent_and_retrievable(question_ui):
    harness, paper_id, client, csrf = question_ui
    created = _submit(client, paper_id, csrf)
    assert created["state"] == "queued" and created["answer"] is None
    again = _submit(client, paper_id, csrf)
    assert again["question"]["id"] == created["question"]["id"]
    assert harness.llm_calls("ask") == 0
    assert harness.run("ask").ok
    result = client.get(f"/api/papers/{paper_id}/qa").json()
    assert len(result) == 1 and result[0]["state"] == "done"
    assert harness.llm_calls("ask") == 1
    statements = result[0]["answer"]["statements"]
    assert statements[0]["citations"][0]["quote"] == "We found it works."
    assert statements[1]["inference"] and statements[1]["citations"] == []
    assert "模型解释机制" in client.get("/api/search", params={"q": "解释机制"}).text


def test_question_and_answer_can_be_saved_as_annotation_with_provenance(question_ui):
    harness, paper_id, client, csrf = question_ui
    question_id = _submit(client, paper_id, csrf)["question"]["id"]
    path = f"/api/papers/{paper_id}/qa/{question_id}/annotation"
    assert client.post(path, headers=csrf).status_code == 422
    assert harness.run("ask").ok
    response = client.post(path, headers=csrf)
    assert response.status_code == 201
    note = response.json()
    assert note["kind"] == "note" and note["page"] == 1
    assert note["body"] == "方法为什么有效？"
    assert note["quote"] == "We found it works."
    assert note["qa_question_id"] == question_id
    source = client.get(f"/api/papers/{paper_id}/qa/{note['qa_question_id']}").json()
    assert source["answer"]["statements"][0]["text"] == "模型解释机制"
    hits = client.get("/api/search", params={"q": "模型解释机制"}).json()["hits"]
    assert hits and all(hit["authorship"] == "model" for hit in hits)


def test_qa_rejects_csrf_cross_paper_ids_and_conflicting_request_ids(question_ui):
    harness, paper_id, client, csrf = question_ui
    path = f"/api/papers/{paper_id}/qa"
    body = {"text": "问题", "request_id": "question-1"}
    assert client.post(path, json=body).status_code == 403
    question_id = _submit(client, paper_id, csrf)["question"]["id"]
    assert client.post(path, json=body, headers=csrf).status_code == 422
    other = next(
        item for item in harness.ctx.store.judgments.papers_with_scores() if item != paper_id
    )
    assert client.get(f"/api/papers/{other}/qa/{question_id}").status_code == 404
    assert client.get("/api/papers/missing/qa").status_code == 404
    assert harness.llm_calls("ask") == 0


def test_qa_budget_state_is_visible_while_question_stays_pending(question_ui):
    harness, paper_id, client, csrf = question_ui
    question_id = _submit(client, paper_id, csrf)["question"]["id"]
    # create_app 固定同一ctx；网关预算来自数据库，记一条真实结构的测试账单。
    harness.ctx.store.runs.record_llm_call(
        LLMCall("configuration_test", "test-model", "test", 1, 1, 10, True),
        harness.ctx.now_iso(),
    )
    response = client.get(f"/api/papers/{paper_id}/qa/{question_id}")
    assert response.json()["state"] == response.json()["code"] == "budget_exceeded"
    assert harness.run("ask").budget_stopped
    assert harness.ctx.store.jobs.get(f"ask:{question_id}").status == "pending"
    assert harness.llm_calls("ask") == 0
