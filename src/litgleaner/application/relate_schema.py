"""一次冻结候选对的关系输出协议，原文核验由应用执行。"""

from collections.abc import Mapping
from typing import Any

RELATE_CONTRACT = "relate-v1"
KINDS = (
    "extends",
    "uses_method_of",
    "compares_with",
    "contradicts",
    "similar_approach",
    "same_problem",
)


def _object(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": dict(properties),
    }


def _text(maximum: int) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": r"\S"}


_EVIDENCE = _object(
    {
        "paper_id": _text(200),
        "doc_key": _text(2000),
        "page": {"type": "integer", "minimum": 0},
        "quote": {**_text(3000), "minLength": 10},
    }
)
_OPTIONAL_EVIDENCE = {"anyOf": [_EVIDENCE, {"type": "null"}]}
RELATION_SCHEMA = _object(
    {
        "paper_a": _text(200),
        "paper_b": _text(200),
        "kind": {"type": ["string", "null"], "enum": [*KINDS, None]},
        "evidence_a": _OPTIONAL_EVIDENCE,
        "evidence_b": _OPTIONAL_EVIDENCE,
        "explanation": _text(2000),
    }
)
RELATE_SCHEMA: Mapping[str, Any] = _object(
    {"relations": {"type": "array", "maxItems": 200, "items": RELATION_SCHEMA}}
)
