"""问答输出保留原文引用，并单独标识用户记忆及明确论文提及。"""

from collections.abc import Mapping
from typing import Any

_QUOTE = {"type": "string", "maxLength": 2000}
_IDENTITY = {"type": "string", "minLength": 1, "maxLength": 200}
ANSWER_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["statements"],
    "properties": {
        "statements": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "citations"],
                "properties": {
                    "text": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "citations": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["paper_id", "page", "quote"],
                            "properties": {
                                "paper_id": _IDENTITY,
                                "page": {"type": "integer", "minimum": 0},
                                "quote": _QUOTE,
                            },
                        },
                    },
                    "note_citations": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["paper_id", "source_id", "kind", "quote"],
                            "properties": {
                                "paper_id": _IDENTITY,
                                "source_id": _IDENTITY,
                                "kind": {"enum": ["note", "question"]},
                                "quote": _QUOTE,
                            },
                        },
                    },
                    "mentioned_paper_ids": {
                        "type": "array",
                        "maxItems": 8,
                        "uniqueItems": True,
                        "items": _IDENTITY,
                    },
                },
            },
        }
    },
}
