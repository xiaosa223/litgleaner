"""已读综述的冻结输入、费用 scope、只追加结果及本地编号校验。"""

import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.application.outline_input import (
    input_hash,
    paper_record,
    producer_version,
    read_sources,
    source_paper,
)
from litgleaner.application.qa_context import qa_token_count
from litgleaner.application.stages.base import Stage
from litgleaner.domain.errors import PermanentError
from litgleaner.domain.evidence import wrap_untrusted
from litgleaner.domain.language import language_name
from litgleaner.domain.outline import OutlinePaper, TopicOutline, validate_outline
from litgleaner.ports.store import JobSpec

_IDS = {
    "type": "array",
    "maxItems": 1000,
    "items": {"type": "string", "minLength": 1, "maxLength": 200},
}
OUTLINE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["groups", "gaps"],
    "properties": {
        "groups": {
            "type": "array",
            "minItems": 0,
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "description", "goal_ids", "paper_ids"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 120},
                    "description": {"type": "string", "minLength": 1, "maxLength": 600},
                    "goal_ids": _IDS,
                    "paper_ids": _IDS,
                },
            },
        },
        "gaps": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["description", "paper_ids"],
                "properties": {
                    "description": {"type": "string", "minLength": 1, "maxLength": 500},
                    "paper_ids": _IDS,
                },
            },
        },
    },
}


def _guard(ctx: AppContext, p: Mapping[str, Any]) -> None:
    topic = ctx.topics.get(p["topic_id"])
    if (
        topic is None
        or topic.topic_hash != p["topic_hash"]
        or ctx.settings.reading_language != p["reading_language"]
        or ctx.settings.outline.token_budget != p["token_budget"]
        or producer_version(ctx) != p["producer_version"]
    ):
        raise PermanentError("综述配置已变化，请重新预览并确认生成")
    if not _same_sources(ctx, p):
        raise PermanentError("综述来源或已读状态已变化，请重新预览并确认生成")
    system = ctx.llm.require_route("outline").prompt.render(
        {
            "output_language": language_name(ctx.settings.reading_language),
        }
    )
    expected = input_hash(
        p["inputs"], system, p["token_budget"], p["source_hashes"], p["reading_language"]
    )
    if (
        expected != p["input_hash"]
        or qa_token_count(system + wrap_untrusted(p["inputs"])) > p["token_budget"]
    ):
        raise PermanentError("综述冻结输入不完整或超过预算，请重新预览")


def _same_sources(ctx: AppContext, p: Mapping[str, Any]) -> bool:
    topic = ctx.topics[p["topic_id"]]
    snapshot, papers = read_sources(ctx, topic)
    by_id = {paper.id: paper for paper in papers}
    ids = p["input_paper_ids"]
    if len(set(ids)) != len(ids) or not set(ids) <= set(by_id):
        return False
    attachments, hashes, records = [], {}, []
    for id_ in ids:
        attachment, value = source_paper(ctx, topic, by_id[id_], snapshot)
        attachments.append(attachment.model_dump(mode="json"))
        hashes[id_] = value
        records.append(paper_record(ctx, topic, by_id[id_], snapshot))
    expected = {"topic_profile": topic.profile_text(), "papers": records}
    eligible = [record["paper_id"] for record in records if record["eligible_gap_evidence"]]
    try:
        same_input = json.loads(p["inputs"]) == expected
    except (ValueError, TypeError):
        return False
    return (
        hashes == p["source_hashes"]
        and attachments == p["papers"]
        and same_input
        and eligible == list(p["eligible_gap_ids"])
        and list(p["goal_ids"]) == [goal.id for goal in topic.goals]
    )


class OutlineStage(Stage):
    name = "outline"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        p = payload
        if (
            ctx.store.topic_outlines.by_fingerprint(
                p["topic_id"], p["topic_hash"], p["input_hash"], p["producer_version"]
            )
            is not None
        ):
            return
        ctx.llm.require_route("outline")
        _guard(ctx, p)
        result = replace(ctx.llm, scope_id=p["scope_id"]).call(
            "outline",
            {"output_language": language_name(p["reading_language"])},
            p["inputs"],
            OUTLINE_SCHEMA,
        )
        groups, gaps, warnings = validate_outline(
            result.data,
            tuple(p["input_paper_ids"]),
            tuple(p["goal_ids"]),
            tuple(p["eligible_gap_ids"]),
        )
        assigned = {id_ for group in groups for id_ in group.paper_ids}
        ctx.store.topic_outlines.add(
            TopicOutline(
                id=str(ULID()),
                topic_id=p["topic_id"],
                topic_hash=p["topic_hash"],
                input_hash=p["input_hash"],
                producer_version=p["producer_version"],
                model=result.model,
                reading_language=p["reading_language"],
                token_budget=p["token_budget"],
                created_at=ctx.now_iso(),
                scope_id=p["scope_id"],
                considered_paper_ids=tuple(p["considered_paper_ids"]),
                input_paper_ids=tuple(p["input_paper_ids"]),
                source_hashes=dict(p["source_hashes"]),
                goal_ids=tuple(p["goal_ids"]),
                groups=groups,
                gaps=gaps,
                papers=tuple(OutlinePaper.model_validate(item) for item in p["papers"]),
                ungrouped=tuple(id_ for id_ in p["considered_paper_ids"] if id_ not in assigned),
                warnings=tuple(dict.fromkeys((*p["warnings"], *warnings))),
            )
        )
