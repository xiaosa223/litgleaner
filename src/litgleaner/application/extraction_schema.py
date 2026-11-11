"""原文证据提取协议；核验状态与文档坐标只由应用补充。"""

from collections.abc import Mapping
from typing import Any

EXTRACTION_CONTRACT_VERSION = "v2"


def _text(maximum: int) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum, "pattern": r"\S"}


def _object(properties: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": dict(properties),
    }


def _nullable(schema: Mapping[str, Any]) -> dict[str, Any]:
    return {"anyOf": [dict(schema), {"type": "null"}]}


def _array(item: Mapping[str, Any], maximum: int, minimum: int = 0) -> dict[str, Any]:
    return {"type": "array", "minItems": minimum, "maxItems": maximum, "items": dict(item)}


def _limitation(source: str, *, quoted: bool) -> dict[str, Any]:
    return _object(
        {
            "text": _text(400),
            "source": {"type": "string", "enum": [source]},
            "quote": _text(800) if quoted else {"type": "null"},
            "page": {"type": "integer", "minimum": 0} if quoted else {"type": "null"},
        }
    )


_PAGE = {"type": "integer", "minimum": 0}
_EVIDENCE = _object({"text": _text(800), "quote": _text(2000), "page": _PAGE})
_EVIDENCE_REF = {"$ref": "#/$defs/evidence"}

EXTRACT_SCHEMA: Mapping[str, Any] = {
    **_object(
        {
            "summary": {"type": ["string", "null"], "maxLength": 300},
            "conclusion": {"type": ["string", "null"], "maxLength": 800},
            "innovation": {"type": ["string", "null"], "maxLength": 300},
            "key_concepts": _array(
                _object(
                    {
                        "name": _text(200),
                        "role": {"type": "string", "enum": ["core", "prerequisite", "mentioned"]},
                        "definition": _nullable(_text(2000)),
                        "page": _PAGE,
                    }
                ),
                12,
            ),
            "limitations": _array(
                {
                    "anyOf": [
                        _limitation("author", quoted=True),
                        _limitation("inferred", quoted=False),
                        _limitation("inferred", quoted=True),
                    ]
                },
                10,
            ),
            "problem": _nullable(_EVIDENCE_REF),
            "prior_gap": _nullable(_EVIDENCE_REF),
            "approach": _nullable(_EVIDENCE_REF),
            "method": _nullable(_EVIDENCE_REF),
            "claims": _array(_EVIDENCE_REF, 20),
            "datasets": _array(_EVIDENCE_REF, 12),
            "metrics": _array(_EVIDENCE_REF, 16),
            "results": _array(_EVIDENCE_REF, 24),
            "code_data": _array(
                _object(
                    {
                        "kind": {"type": "string", "enum": ["code", "data"]},
                        "availability": {
                            "type": "string",
                            "enum": ["public", "not_public", "unknown"],
                        },
                        "text": _text(800),
                        "url": _nullable(_text(2048)),
                        "quote": _nullable(_text(2000)),
                        "page": _nullable(_PAGE),
                    }
                ),
                16,
            ),
            "questions": _array(
                _object(
                    {
                        "question": _text(800),
                        "answer": _nullable(_EVIDENCE_REF),
                    }
                ),
                3,
                3,
            ),
            "highlights": _array(
                _object(
                    {
                        "kind": {
                            "type": "string",
                            "enum": ["goal", "innovation", "method", "result"],
                        },
                        "quote": _text(2000),
                        "page": _PAGE,
                    }
                ),
                24,
            ),
        }
    ),
    "$defs": {"evidence": _EVIDENCE},
}
