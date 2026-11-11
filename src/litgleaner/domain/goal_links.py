"""论文与研究目标的关联理由，独立于阅读价值算分。"""

from typing import Any

from pydantic import Field

from litgleaner.domain.base import FrozenModel


class GoalLink(FrozenModel):
    goal_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    reuse: str = Field(min_length=1, max_length=1000)
    quote: str | None = Field(default=None, max_length=1600)
    page: int | None = Field(default=None, ge=0, strict=True)
    verified: bool = False


def goal_links_schema() -> dict[str, Any]:
    """verified 由代码设置，模型只提供理由与可定位的原文。"""
    return {
        "type": "array",
        "maxItems": 12,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["goal_id", "reuse", "quote", "page"],
            "properties": {
                "goal_id": {"type": "string", "pattern": r"^[A-Za-z0-9_-]+$"},
                "reuse": {"type": "string", "minLength": 1, "maxLength": 1000},
                "quote": {"type": ["string", "null"], "maxLength": 1600},
                "page": {"type": ["integer", "null"], "minimum": 0},
            },
        },
    }
