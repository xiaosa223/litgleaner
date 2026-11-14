"""导读 → 回答卡片 → 评价 → 掌握程度 → 复习，贯通接口与按需阶段（第 12.7 节）。"""

import json

import pytest
from fastapi.testclient import TestClient

from litgleaner.application.export import mine
from litgleaner.domain.errors import PermanentError
from litgleaner.domain.export import export_name
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.conftest import Harness, extraction_answer, paper


def _client(harness: Harness) -> TestClient:
    client = TestClient(
        create_app(harness.ctx, LocalSecurity(48133, launch_token="guide-test"), None),
        base_url="http://127.0.0.1:48133",
    )
    client.get("/launch", params={"token": "guide-test"})
    client.headers["X-CSRF-Token"] = client.get("/api/session").json()["csrf_token"]
    return client


def _enable(harness: Harness) -> None:
    def change(data: dict) -> None:
        data["tasks"]["guide"] = {"provider": "main", "model": "strong", "prompt": "guide"}
        data["tasks"]["evaluate"] = {"provider": "main", "model": "strong", "prompt": "evaluate"}

    harness.edit_yaml("llm.yaml", change)
    harness.rebuild()


@pytest.fixture
def guided(harness: Harness):
    harness.write_papers([paper("Guide One", 5)])
    harness.llm.overrides["extract"] = {
        **extraction_answer(),
        "key_concepts": [
            {"name": "problem", "role": "core", "definition": "We study the problem.", "page": 0},
            {"name": "works", "role": "mentioned", "definition": None, "page": 1},
        ],
    }
    assert harness.run().ok
    _enable(harness)
    with _client(harness) as client:
        paper_id = client.get("/api/knowledge/concepts").json()["items"][0]
        detail = client.get(f"/api/knowledge/concepts/{paper_id['concept']['id']}").json()
        yield harness, client, detail["occurrences"][0]["occurrence"]["paper_id"]


def _generate(harness: Harness, client: TestClient, paper_id: str) -> dict:
    page = client.get(f"/api/papers/{paper_id}/guide").json()
    assert page["view"]["guide"] is None and page["view"]["plan"]["configured"]
    assert page["view"]["plan"]["knowledge_empty"] is True
    before = harness.llm_calls("guide")
    queued = client.post(
        f"/api/papers/{paper_id}/guide", json={"plan_hash": page["view"]["plan"]["plan_hash"]}
    )
    assert queued.status_code == 202, queued.text
    assert queued.json()["view"]["job"] == "queued"
    assert harness.llm_calls("guide") == before  # 排队本身不调用模型
    assert harness.run(only="guide").ok
    return client.get(f"/api/papers/{paper_id}/guide").json()


def test_guide_is_generated_once_with_three_questions(guided):
    harness, client, paper_id = guided
    client.post(f"/api/papers/{paper_id}/open")  # 正在读的这篇不算“见过”
    page = _generate(harness, client, paper_id)
    guide = page["view"]["guide"]
    assert [q["kind"] for q in guide["questions"]] == ["understand", "compare", "transfer"]
    assert guide["questions"][2]["goal_id"] == "g1"
    sent = next(c for c in harness.llm.calls if c.task == "guide").user
    assert '"profile_match": "relevant"' in sent  # 初筛判为相关的论文才按课题目标出迁移题
    assert {c["treatment"] for c in guide["concepts"]} == {"full"}
    assert page["view"]["stale"] is False
    for citation in guide["overview"]["citations"]:
        assert citation["page"] >= 0 and citation["quote"]
    calls = harness.llm_calls("guide")
    # 已缓存：再次确认不会重复排队
    client.post(
        f"/api/papers/{paper_id}/guide", json={"plan_hash": page["view"]["plan"]["plan_hash"]}
    )
    assert harness.run(only="guide").ok
    assert harness.llm_calls("guide") == calls


def test_stale_plan_hash_is_rejected(guided):
    _, client, paper_id = guided
    response = client.post(f"/api/papers/{paper_id}/guide", json={"plan_hash": "old"})
    assert response.status_code == 422


def test_answer_evaluation_raises_mastery_and_schedules_review(guided):
    harness, client, paper_id = guided
    guide = _generate(harness, client, paper_id)["view"]["guide"]
    concept_id = guide["questions"][0]["concept_ids"][0]
    page = client.post(
        f"/api/papers/{paper_id}/guide/{guide['id']}/answers",
        json={"question_index": 0, "text": "它研究这个问题的核心做法。"},
    ).json()
    card = page["cards"][0]
    assert card["card"]["revision"] == 1 and card["evaluation"] is None
    assert card["evaluation_state"] == "queued"
    assert harness.run(only="evaluate").ok
    card = client.get(f"/api/papers/{paper_id}/guide").json()["cards"][0]
    assert card["evaluation"]["overall"] == "correct"
    assert card["evaluation"]["verdicts"][0]["concept_id"] == concept_id
    cards = client.get("/api/knowledge/cards").json()
    item = next(i for i in cards["items"] if i["summary"]["concept"]["id"] == concept_id)
    assert item["mastery"]["level"] == 2
    assert item["answers"][0]["text"] == "它研究这个问题的核心做法。"
    assert not item["due"]
    harness.clock.advance(days=1)
    due = client.get("/api/knowledge/review").json()
    assert [d["summary"]["concept"]["id"] for d in due] == [concept_id]
    state = client.post(
        f"/api/knowledge/concepts/{concept_id}/review", json={"rating": "remembered"}
    ).json()
    assert state["step"] == 1
    assert client.get("/api/knowledge/review").json() == []


def test_revisions_append_and_wrong_answers_do_not_raise_level(guided):
    harness, client, paper_id = guided
    guide = _generate(harness, client, paper_id)["view"]["guide"]
    url = f"/api/papers/{paper_id}/guide/{guide['id']}/answers"
    client.post(url, json={"question_index": 0, "text": "草稿", "evaluate": False})
    page = client.post(url, json={"question_index": 0, "text": "[[wrong]] 理解偏了"}).json()
    assert page["cards"][0]["revisions"] == 2
    assert harness.run(only="evaluate").ok
    card = client.get(f"/api/papers/{paper_id}/guide").json()["cards"][0]
    assert card["evaluation"]["verdicts"][0]["verdict"] == "misunderstood"
    concept_id = guide["questions"][0]["concept_ids"][0]
    item = next(
        i
        for i in client.get("/api/knowledge/cards").json()["items"]
        if i["summary"]["concept"]["id"] == concept_id
    )
    assert item["mastery"]["level"] <= 1
    assert item["answers"][0]["verdict"] == "misunderstood"
    wrong = client.post(
        f"/api/papers/{paper_id}/answers/{card['card']['id']}/wrong", json={"reason": "评价错了"}
    )
    assert wrong.status_code == 204


def test_user_override_and_knowledge_change_marks_guide_stale(guided):
    harness, client, paper_id = guided
    _generate(harness, client, paper_id)
    concept_id = client.get("/api/knowledge/cards").json()["items"][0]["summary"]["concept"]["id"]
    state = client.post(f"/api/knowledge/concepts/{concept_id}/level", json={"level": 3}).json()
    assert state["level"] == 3 and state["overridden"]
    page = client.get(f"/api/papers/{paper_id}/guide").json()
    assert page["view"]["stale"] is True  # 知识库变了，提示可以重新生成
    assert page["view"]["guide"] is not None
    bad = client.post(f"/api/knowledge/concepts/{concept_id}/level", json={"level": 4})
    assert bad.status_code == 422


def test_guide_requires_model_configuration(harness: Harness):
    harness.write_papers([paper("Guide None", 5)])
    harness.llm.overrides["extract"] = {
        **extraction_answer(),
        "key_concepts": [
            {"name": "problem", "role": "core", "definition": "We study the problem.", "page": 0}
        ],
    }
    assert harness.run().ok
    with _client(harness) as client:
        item = client.get("/api/knowledge/concepts").json()["items"][0]
        detail = client.get(f"/api/knowledge/concepts/{item['concept']['id']}").json()
        paper_id = detail["occurrences"][0]["occurrence"]["paper_id"]
        page = client.get(f"/api/papers/{paper_id}/guide").json()
        assert page["view"]["plan"]["configured"] is False
        assert page["evaluate_configured"] is False
        response = client.post(f"/api/papers/{paper_id}/guide", json={"plan_hash": "x"})
        assert response.status_code == 409
        assert response.json()["task"] == "guide"


def test_guide_waits_for_extraction(harness: Harness):
    harness.write_papers([paper("Guide Waiting", 5)])
    harness.llm.fail_tasks["extract"] = [PermanentError("bad")] * 5
    harness.run()
    _enable(harness)
    with _client(harness) as client:
        hits = client.get("/api/search", params={"q": "Guide Waiting"}).json()["hits"]
        paper_id = hits[0]["paper_id"]
        page = client.get(f"/api/papers/{paper_id}/guide").json()
        assert page["view"]["guide"] is None
        assert "提炼" in page["view"]["unavailable"]


def test_partially_wrong_answers_are_not_quoted_as_anchor(guided):
    harness, client, paper_id = guided
    guide = _generate(harness, client, paper_id)["view"]["guide"]
    url = f"/api/papers/{paper_id}/guide/{guide['id']}/answers"
    client.post(url, json={"question_index": 0, "text": "完全正确的理解。"})
    client.post(url, json={"question_index": 1, "text": "[[partial]] 漏了一点"})
    assert harness.run(only="evaluate").ok
    concept_id = guide["questions"][0]["concept_ids"][0]
    item = next(
        i
        for i in client.get("/api/knowledge/cards").json()["items"]
        if i["summary"]["concept"]["id"] == concept_id
    )
    assert item["answers"][0]["text"] == "完全正确的理解。"
    assert item["answers"][-1]["overall"] == "partial"


def test_identical_resubmit_neither_adds_revision_nor_charges_again(guided):
    harness, client, paper_id = guided
    guide = _generate(harness, client, paper_id)["view"]["guide"]
    url = f"/api/papers/{paper_id}/guide/{guide['id']}/answers"
    client.post(url, json={"question_index": 0, "text": "同样的回答"})
    page = client.post(url, json={"question_index": 0, "text": " 同样的回答 "}).json()
    assert page["cards"][0]["revisions"] == 1
    assert harness.run(only="evaluate").ok
    assert harness.llm_calls("evaluate") == 1


def test_evaluation_missing_a_concept_is_retried_not_saved(guided):
    harness, client, paper_id = guided
    guide = _generate(harness, client, paper_id)["view"]["guide"]
    harness.llm.overrides["evaluate"] = {
        "overall": "correct",
        "verdicts": [],
        "reference": {"text": "参考", "citations": []},
        "advice": "",
    }
    url = f"/api/papers/{paper_id}/guide/{guide['id']}/answers"
    client.post(url, json={"question_index": 0, "text": "回答"})
    harness.run(only="evaluate")
    card = client.get(f"/api/papers/{paper_id}/guide").json()["cards"][0]
    assert card["evaluation"] is None
    assert card["evaluation_state"] in {"retrying", "failed"}


def test_dead_guide_job_can_be_retried_by_confirming_again(guided):
    harness, client, paper_id = guided
    plan = client.get(f"/api/papers/{paper_id}/guide").json()["view"]["plan"]
    harness.llm.fail_tasks["guide"] = [PermanentError("bad")]
    client.post(f"/api/papers/{paper_id}/guide", json={"plan_hash": plan["plan_hash"]})
    harness.run(only="guide")
    assert client.get(f"/api/papers/{paper_id}/guide").json()["view"]["job"] == "failed"
    page = client.post(f"/api/papers/{paper_id}/guide", json={"plan_hash": plan["plan_hash"]})
    assert page.json()["view"]["job"] == "queued"
    assert harness.run(only="guide").ok
    assert client.get(f"/api/papers/{paper_id}/guide").json()["view"]["guide"] is not None


def test_review_before_due_is_rejected(guided):
    harness, client, paper_id = guided
    guide = _generate(harness, client, paper_id)["view"]["guide"]
    concept_id = guide["questions"][0]["concept_ids"][0]
    client.post(
        f"/api/papers/{paper_id}/guide/{guide['id']}/answers",
        json={"question_index": 0, "text": "正确理解"},
    )
    assert harness.run(only="evaluate").ok
    early = client.post(
        f"/api/knowledge/concepts/{concept_id}/review", json={"rating": "remembered"}
    )
    assert early.status_code == 422


def test_personal_export_includes_answer_revisions_evaluations_and_mastery(guided):
    harness, client, paper_id = guided
    guide = _generate(harness, client, paper_id)["view"]["guide"]
    url = f"/api/papers/{paper_id}/guide/{guide['id']}/answers"
    client.post(url, json={"question_index": 0, "text": "初稿", "evaluate": False})
    client.post(url, json={"question_index": 0, "text": "改过的理解"})
    assert harness.run(only="evaluate").ok
    files = {
        f.name: f.content.decode() for f in mine(harness.ctx) if f.name.endswith((".md", ".json"))
    }
    manifest = json.loads(files["manifest.json"])
    assert manifest["answer_cards"] == 2 and "unavailable_sections" not in manifest
    paper_md = files[export_name("papers", paper_id)]
    assert "第 1 版" in paper_md and "初稿" in paper_md and "第 2 版" in paper_md
    assert "模型评价" in paper_md and "模型参考答案" in paper_md
    concept_id = guide["questions"][0]["concept_ids"][0]
    concept_md = files[export_name("concepts", concept_id)]
    assert "掌握程度（系统按规则算出）：能解释" in concept_md and "改过的理解" in concept_md


def test_marking_familiar_concepts_before_generation_shortens_their_explanation(guided):
    harness, client, paper_id = guided
    plan = client.get(f"/api/papers/{paper_id}/guide").json()["view"]["plan"]
    assert {c["level"] for c in plan["concepts"]} == {0} and len(plan["concepts"]) == 2
    known = plan["concepts"][0]["concept_id"]
    client.post(f"/api/knowledge/concepts/{known}/level", json={"level": 2})
    updated = client.get(f"/api/papers/{paper_id}/guide").json()["view"]["plan"]
    assert updated["plan_hash"] != plan["plan_hash"]
    assert next(c for c in updated["concepts"] if c["concept_id"] == known)["level"] == 2
    client.post(f"/api/papers/{paper_id}/guide", json={"plan_hash": updated["plan_hash"]})
    assert harness.run(only="guide").ok
    guide = client.get(f"/api/papers/{paper_id}/guide").json()["view"]["guide"]
    treatments = {c["concept_id"]: c["treatment"] for c in guide["concepts"]}
    assert treatments[known] == "brief"
    assert set(treatments.values()) == {"brief", "full"}
