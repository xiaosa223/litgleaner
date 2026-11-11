"""地图只执行用户确认的冻结输入；历史地图和新增论文均不会自动排队。"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.application.qa_context import qa_token_count
from litgleaner.application.stages.base import Stage
from litgleaner.application.topic_map_input import map_input_hash, producer_version, source_hash
from litgleaner.domain.errors import PermanentError
from litgleaner.domain.evidence import wrap_untrusted
from litgleaner.domain.language import language_name
from litgleaner.domain.topic_map import TopicMap, validate_groups
from litgleaner.ports.store import JobSpec

MAP_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["groups"],
    "properties": {
        "groups": {
            "type": "array",
            "minItems": 5,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "description", "paper_ids"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 120},
                    "description": {"type": "string", "minLength": 1, "maxLength": 800},
                    "paper_ids": {
                        "type": "array",
                        "maxItems": 1000,
                        "items": {"type": "string", "maxLength": 200},
                    },
                },
            },
        }
    },
}


def _guard(ctx: AppContext, p: Mapping[str, Any]) -> None:
    topic = ctx.topics.get(p["topic_id"])
    if (
        topic is None
        or topic.topic_hash != p["topic_hash"]
        or (
            ctx.settings.reading_language != p["reading_language"]
            or ctx.settings.map.token_budget != p["token_budget"]
            or producer_version(ctx) != p["producer_version"]
        )
    ):
        raise PermanentError("地图配置已变化，请重新预览并确认生成")
    snapshot = ctx.store.topic_maps.snapshot(topic.id, topic.topic_hash)
    hashes = {
        paper.id: source_hash(paper, snapshot.extractions.get(paper.id))
        for paper in snapshot.papers
        if paper.id in p["input_paper_ids"]
    }
    if hashes != p["source_hashes"]:
        raise PermanentError("地图论文输入已变化，请重新预览并确认生成")
    system = ctx.llm.require_route("map").prompt.render(
        {
            "output_language": language_name(ctx.settings.reading_language),
        }
    )
    expected = map_input_hash(
        p["inputs"], system, p["token_budget"], p["source_hashes"], p["reading_language"]
    )
    if (
        expected != p["input_hash"]
        or qa_token_count(system + wrap_untrusted(p["inputs"])) > p["token_budget"]
    ):
        raise PermanentError("地图冻结输入不完整或超过预算，请重新预览")


class TopicMapStage(Stage):
    name = "map"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        return ()

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        p = payload
        existing = ctx.store.topic_maps.by_fingerprint(
            p["topic_id"], p["topic_hash"], p["input_hash"], p["producer_version"]
        )
        if existing is not None:
            return
        ctx.llm.require_route("map")
        _guard(ctx, p)
        scoped = replace(ctx.llm, scope_id=p["scope_id"])
        result = scoped.call(
            "map",
            {"output_language": language_name(p["reading_language"])},
            p["inputs"],
            MAP_SCHEMA,
        )
        groups, warnings = validate_groups(result.data["groups"], tuple(p["input_paper_ids"]))
        assigned = {id_ for group in groups for id_ in group.paper_ids}
        ctx.store.topic_maps.add(
            TopicMap(
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
                groups=groups,
                ungrouped=tuple(id_ for id_ in p["considered_paper_ids"] if id_ not in assigned),
                warnings=tuple(dict.fromkeys((*p["warnings"], *warnings))),
            )
        )
