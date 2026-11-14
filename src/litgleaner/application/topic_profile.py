"""从上传的任务书、开题报告、比赛文档推断研究画像（ADR 0012，第 7.2 节）。

只推断名称、背景、内容、目标，结果交给新建课题表单预填；不写文件、不启用课题。
文档文字是不可信数据，由网关包裹后交给模型。
"""

from dataclasses import dataclass
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.topic_setup import MAX_NAME
from litgleaner.domain.chunker import estimate_tokens
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.language import language_name

TASK = "profile"
PROFILE_TOKEN_BUDGET = 12_000
MAX_TEXT = 8000
MAX_PROFILE_GOALS = 5
_TRUNCATED = "\n\n（后文已截断）"

PROFILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "background", "content", "goals"],
    "properties": {
        "name": {"type": "string", "minLength": 1},
        "background": {"type": "string", "minLength": 1},
        "content": {"type": "string", "minLength": 1},
        "goals": {"type": "array", "minItems": 1, "items": {"type": "string"}},
    },
}


@dataclass(frozen=True)
class ProfileGuess:
    name: str
    background: str
    content: str
    goals: tuple[str, ...]


def _truncate(text: str, budget: int) -> str:
    tokens = estimate_tokens(text)
    if tokens <= budget:
        return text
    keep = max(1, len(text) * budget // tokens)
    return text[:keep].rstrip() + _TRUNCATED


def infer_profile(ctx: AppContext, name: str, content: bytes) -> ProfileGuess:
    """先确认模型已配置（没配置时不读文档），再本地抽文字、调用一次模型。"""
    ctx.llm.require_route(TASK)
    if ctx.documents is None:
        raise InvalidRequest("document_unavailable")
    text = _truncate(ctx.documents.extract(name, content), PROFILE_TOKEN_BUDGET)
    variables = {"output_language": language_name(ctx.settings.reading_language)}
    data = ctx.llm.call(TASK, variables, text, PROFILE_SCHEMA).data
    goals = tuple(goal.strip()[:1000] for goal in data["goals"] if goal.strip())
    return ProfileGuess(
        name=data["name"].strip()[:MAX_NAME],
        background=data["background"].strip()[:MAX_TEXT],
        content=data["content"].strip()[:MAX_TEXT],
        goals=goals[:MAX_PROFILE_GOALS],
    )
