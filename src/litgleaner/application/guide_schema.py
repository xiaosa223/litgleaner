"""guide、evaluate 两个任务的模型输出结构；本地按完整结构校验，引用另由代码核验。"""

from collections.abc import Mapping
from typing import Any


def _text(limit: int) -> Mapping[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": limit}


def _object(properties: Mapping[str, Any]) -> Mapping[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
    }


_NULLABLE_TEXT = {"type": ["string", "null"], "maxLength": 300}
_CITATIONS = {
    "type": "array",
    "maxItems": 4,
    "items": _object({"page": {"type": "integer", "minimum": 0}, "quote": _text(1200)}),
}
_SOURCED = _object({"text": _text(1500), "citations": _CITATIONS})

GUIDE_SCHEMA: Mapping[str, Any] = _object(
    {
        "overview": _SOURCED,
        "concepts": {
            "type": "array",
            "maxItems": 8,
            "items": _object(
                {
                    "concept_id": _text(200),
                    "explanation": _SOURCED,
                    "analogy": {
                        "anyOf": [
                            {"type": "null"},
                            _object(
                                {
                                    "concept_id": _NULLABLE_TEXT,
                                    "name": _text(200),
                                    "text": _text(800),
                                    "breaks": _text(600),
                                }
                            ),
                        ]
                    },
                }
            ),
        },
        "questions": {
            "type": "array",
            "minItems": 3,
            "maxItems": 3,
            "items": _object(
                {
                    "kind": {"enum": ["understand", "compare", "transfer"]},
                    "text": _text(600),
                    "concept_ids": {"type": "array", "maxItems": 4, "items": _text(200)},
                    "section": _NULLABLE_TEXT,
                    "goal_id": _NULLABLE_TEXT,
                }
            ),
        },
        "sections": {
            "type": "array",
            "maxItems": 5,
            "items": _object({"section": _text(300), "why": _text(400)}),
        },
        "relations": {
            "type": "array",
            "maxItems": 6,
            "items": _object(
                {
                    "concept_id": _text(200),
                    "related_concept_id": _text(200),
                    "kind": {"enum": ["prerequisite", "similar", "contrast"]},
                    "reason": _text(300),
                }
            ),
        },
        "duplicates": {
            "type": "array",
            "maxItems": 3,
            "items": _object(
                {
                    "concept_id": _text(200),
                    "same_as_concept_id": _text(200),
                    "reason": _text(300),
                }
            ),
        },
    }
)

EVALUATE_SCHEMA: Mapping[str, Any] = _object(
    {
        "overall": {"enum": ["correct", "partial", "incorrect"]},
        "verdicts": {
            "type": "array",
            "maxItems": 4,
            "items": _object(
                {
                    "concept_id": _text(200),
                    "verdict": {"enum": ["correct", "missing", "misunderstood"]},
                    "comment": _text(600),
                    "citations": _CITATIONS,
                }
            ),
        },
        "reference": _SOURCED,
        "advice": {"type": "string", "maxLength": 400},
    }
)
