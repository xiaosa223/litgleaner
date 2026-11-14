"""概念关系与疑似重复：随导读提议，用户确认或删除；确认合并后知识库按一个概念显示（第 12.7 节）。"""

import pytest

from tests.e2e.conftest import Harness, extraction_answer, paper
from tests.e2e.test_guidance_api import _client, _enable


def _concepts(*names: str) -> dict:
    return {
        **extraction_answer(),
        "key_concepts": [
            {"name": n, "role": "core", "definition": "We study the problem.", "page": 0}
            for n in names
        ],
    }


def _guide(harness: Harness, client, paper_id: str) -> dict:
    plan = client.get(f"/api/papers/{paper_id}/guide").json()["view"]["plan"]
    client.post(f"/api/papers/{paper_id}/guide", json={"plan_hash": plan["plan_hash"]})
    assert harness.run(only="guide").ok
    return client.get(f"/api/papers/{paper_id}/guide").json()["view"]["guide"]


def _paper_id(client, name: str) -> str:
    item = next(
        i
        for i in client.get("/api/knowledge/concepts").json()["items"]
        if i["concept"]["name"] == name
    )
    detail = client.get(f"/api/knowledge/concepts/{item['concept']['id']}").json()
    return detail["occurrences"][-1]["occurrence"]["paper_id"]


@pytest.fixture
def linked(harness: Harness):
    harness.write_papers(
        [paper("Self-Attention with Softmax", 5), paper("Attention Duplicate Study", 5)]
    )
    # 提取只保留在各自论文正文里出现的名字：第一篇得到前两个，第二篇得到最后一个
    harness.llm.overrides["extract"] = _concepts("Self-Attention", "Softmax", "Attention Duplicate")
    assert harness.run().ok
    _enable(harness)
    with _client(harness) as client:
        yield harness, client


def test_guide_proposes_relations_that_users_confirm_or_delete(linked):
    harness, client = linked
    first = _paper_id(client, "Self-Attention")
    _guide(harness, client, first)
    cards = {
        c["summary"]["concept"]["name"]: c
        for c in client.get("/api/knowledge/cards").json()["items"]
    }
    related = cards["Self-Attention"]["related"]
    assert len(related) == 1 and related[0]["kind"] == "prerequisite"
    assert related[0]["name"] == "Softmax" and related[0]["confirmed"] is False
    assert cards["Softmax"]["related"][0]["direction"] == "in"
    link = related[0]["link_id"]
    assert (
        client.post(
            "/api/knowledge/decisions", json={"target": link, "decision": "confirmed"}
        ).status_code
        == 204
    )
    cards = {
        c["summary"]["concept"]["name"]: c
        for c in client.get("/api/knowledge/cards").json()["items"]
    }
    assert cards["Self-Attention"]["related"][0]["confirmed"] is True
    client.post("/api/knowledge/decisions", json={"target": link, "decision": "rejected"})
    cards = {
        c["summary"]["concept"]["name"]: c
        for c in client.get("/api/knowledge/cards").json()["items"]
    }
    assert cards["Self-Attention"]["related"] == []
    assert (
        client.post(
            "/api/knowledge/decisions", json={"target": "nope", "decision": "confirmed"}
        ).status_code
        == 404
    )


def test_confirmed_merge_shows_one_concept_with_combined_record(linked):
    harness, client = linked
    first = _paper_id(client, "Self-Attention")
    guide = _guide(harness, client, first)
    url = f"/api/papers/{first}/guide/{guide['id']}/answers"
    client.post(url, json={"question_index": 0, "text": "自注意力的正确理解"})
    assert harness.run(only="evaluate").ok  # Self-Attention 到“能解释”，成为已掌握概念
    second = _paper_id(client, "Attention Duplicate")
    _guide(harness, client, second)
    merges = client.get("/api/knowledge/merges").json()
    assert [(m["from_name"], m["into_name"]) for m in merges] == [
        ("Attention Duplicate", "Self-Attention")
    ]
    client.post(
        "/api/knowledge/decisions", json={"target": merges[0]["merge_id"], "decision": "confirmed"}
    )
    assert client.get("/api/knowledge/merges").json() == []
    names = [
        c["summary"]["concept"]["name"] for c in client.get("/api/knowledge/cards").json()["items"]
    ]
    assert "Attention Duplicate" not in names
    card = next(
        c
        for c in client.get("/api/knowledge/cards").json()["items"]
        if c["summary"]["concept"]["name"] == "Self-Attention"
    )
    assert [m["name"] for m in card["merged"]] == ["Attention Duplicate"]
    assert card["mastery"]["level"] == 2
    # 撤销合并：追加一条“不是”，重复概念重新单列
    undo = client.post(
        "/api/knowledge/decisions",
        json={"target": card["merged"][0]["merge_id"], "decision": "rejected"},
    )
    assert undo.status_code == 204
    names = [
        c["summary"]["concept"]["name"] for c in client.get("/api/knowledge/cards").json()["items"]
    ]
    assert "Attention Duplicate" in names
