"""起草检索式的按需阶段：只执行研究者确认过的冻结输入，配置变了就停下，不花钱。"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.stages.base import Stage
from litgleaner.application.topic_draft import STAGE, current_plan_hash, producer_version
from litgleaner.domain.errors import NotFound, PermanentError
from litgleaner.domain.language import language_name
from litgleaner.domain.topic_draft import (
    MAX_EXCLUDE,
    MAX_GLOSSARY,
    MAX_INCLUDE,
    MAX_SYNONYMS,
    draft_from_answer,
    profile_hash,
)
from litgleaner.ports.store import JobSpec


def _text(limit: int) -> Mapping[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": limit}


def _items(properties: Mapping[str, Any], most: int) -> Mapping[str, Any]:
    return {
        "type": "array",
        "maxItems": most,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        },
    }


DRAFT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["include", "exclude", "glossary"],
    "properties": {
        "include": {
            **_items(
                {
                    "term": _text(120),
                    "reason": _text(600),
                    "synonyms": {"type": "array", "maxItems": MAX_SYNONYMS, "items": _text(120)},
                },
                MAX_INCLUDE,
            ),
            "minItems": 1,
        },
        "exclude": _items({"term": _text(120), "reason": _text(600)}, MAX_EXCLUDE),
        "glossary": _items(
            {"term": _text(120), "translation": _text(120), "definition": _text(300)},
            MAX_GLOSSARY,
        ),
    },
}


def _guard(ctx: AppContext, p: Mapping[str, Any]) -> None:
    topic = ctx.topics.get(p["topic_id"])
    if (
        topic is None
        or profile_hash(topic) != p["profile_hash"]
        or ctx.settings.reading_language != p["reading_language"]
        or producer_version(ctx) != p["producer_version"]
    ):
        raise PermanentError("课题描述或模型配置已变化，请重新预览并确认起草")
    if ctx.topic_drafts is None:
        raise PermanentError("没有保存草稿的位置")
    try:
        current = current_plan_hash(ctx, p["topic_id"])
    except NotFound:
        raise PermanentError("课题已不存在") from None
    if current != p["plan_hash"]:  # 同一课题先后确认了两份计划时，只有最新的那份会调用模型
        raise PermanentError("课题的检索式或种子论文已变化，请重新预览并确认起草")


class TopicDraftStage(Stage):
    name = STAGE

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        p = payload
        saved = ctx.topic_drafts.load(p["topic_id"]) if ctx.topic_drafts else None
        if saved is not None and saved.plan_hash == p["plan_hash"]:
            return
        ctx.llm.require_route(STAGE)
        _guard(ctx, p)
        scoped = replace(ctx.llm, scope_id=p["scope_id"])
        variables = {"output_language": language_name(p["reading_language"])}
        result = scoped.call(STAGE, variables, p["inputs"], DRAFT_SCHEMA)
        draft = draft_from_answer(
            result.data,
            topic_id=p["topic_id"],
            plan_hash=p["plan_hash"],
            profile_hash=p["profile_hash"],
            model=result.model,
            created_at=ctx.now_iso(),
            reading_language=p["reading_language"],
            seed_titles=tuple(p["seed_titles"]),
        )
        assert ctx.topic_drafts is not None  # _guard 已检查
        ctx.topic_drafts.save(draft)
