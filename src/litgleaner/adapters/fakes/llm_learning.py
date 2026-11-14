"""假模型的导读与回答评价：按输入确定地生成，引用取自发送的原文，便于端到端核验。

回答里写 [[wrong]] 时判为理解偏了，写 [[partial]] 时判为有遗漏。
"""

import json
import re
from collections.abc import Mapping
from typing import Any

_UNTRUSTED = re.compile(r"</?untrusted_paper>")
_SOURCE = re.compile(r"\[page=(\d+)\]\n(.+)")


def _data(user: str) -> Mapping[str, Any]:
    loaded: Mapping[str, Any] = json.loads(_UNTRUSTED.sub("", user))
    return loaded


def _citations(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    match = _SOURCE.search(str(data.get("paper_sources", "")))
    if match is None:
        return []
    words = match.group(2).split()
    if len(words) < 4:
        return []
    return [{"page": int(match.group(1)), "quote": " ".join(words[: min(12, len(words))])}]


def _explanation(row: Mapping[str, Any], cited: list[dict[str, Any]]) -> Mapping[str, Any]:
    texts = {
        "full": f"演示讲解：{row['name']} 是本文的关键概念。",
        "remind": f"演示提醒：你见过 {row['name']}，本文的用法略有不同。",
        "brief": f"演示带过：{row['name']} 你已经掌握。",
    }
    return {"text": texts.get(row["treatment"], texts["full"]), "citations": cited}


def _questions(ids: list[str], sections: list[str], goals: list[Any]) -> list[Mapping[str, Any]]:
    return [
        {
            "kind": "understand",
            "text": "演示理解题：用自己的话说说核心机制。",
            "concept_ids": ids,
            "section": sections[0] if sections else None,
            "goal_id": None,
        },
        {
            "kind": "compare",
            "text": "演示对比题：和已有方法有何异同？",
            "concept_ids": ids,
            "section": None,
            "goal_id": None,
        },
        {
            "kind": "transfer",
            "text": "演示迁移题：能用到你的课题上吗？",
            "concept_ids": ids,
            "section": None,
            "goal_id": goals[0]["id"] if goals else None,
        },
    ]


def fake_guide(user: str) -> Mapping[str, Any]:
    data = _data(user)
    cited = _citations(data)
    concepts = list(data.get("concepts", ()))
    known = list(data.get("known_concepts", ()))
    ids = [row["concept_id"] for row in concepts][:1]
    goals = list(data.get("goals", ()))
    sections = list(data.get("sections", ()))
    analogy = (
        {
            "concept_id": known[0]["concept_id"],
            "name": known[0]["name"],
            "text": "演示类比：两者都在做加权求和。",
            "breaks": "演示失效点：权重来源不同。",
        }
        if known
        else None
    )
    return {
        "overview": {"text": f"演示导读：{data['paper']['title']}", "citations": cited},
        "concepts": [
            {
                "concept_id": row["concept_id"],
                "explanation": _explanation(row, cited),
                "analogy": analogy if row["treatment"] == "full" else None,
            }
            for row in concepts
        ],
        "questions": _questions(ids, sections, goals),
        "sections": [{"section": s, "why": "演示：先读这里。"} for s in sections[:2]],
        "relations": [
            {
                "concept_id": concepts[0]["concept_id"],
                "related_concept_id": concepts[1]["concept_id"],
                "kind": "prerequisite",
                "reason": "演示关系：理解前者要先懂后者。",
            }
        ]
        if len(concepts) > 1
        else [],
        "duplicates": [
            {
                "concept_id": row["concept_id"],
                "same_as_concept_id": known[0]["concept_id"],
                "reason": "演示：同一概念的不同写法。",
            }
            for row in concepts
            if known and "duplicate" in row["name"].casefold()
        ],
    }


def fake_evaluate(user: str) -> Mapping[str, Any]:
    data = _data(user)
    answer = str(data.get("user_answer", ""))
    verdict = (
        "misunderstood"
        if "[[wrong]]" in answer
        else "missing"
        if "[[partial]]" in answer
        else "correct"
    )
    overall = {"correct": "correct", "missing": "partial", "misunderstood": "incorrect"}[verdict]
    cited = _citations(data)
    return {
        "overall": overall,
        "verdicts": [
            {
                "concept_id": row["concept_id"],
                "verdict": verdict,
                "comment": "演示评价。",
                "citations": cited,
            }
            for row in data.get("concepts", ())
        ],
        "reference": {"text": "演示参考答案。", "citations": cited},
        "advice": "演示建议：回到方法一节再看看。",
    }
