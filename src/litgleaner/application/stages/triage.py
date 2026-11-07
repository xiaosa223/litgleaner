"""初筛阶段：便宜模型只看标题和摘要。"""

from collections.abc import Iterable, Mapping
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.documents import paper_header
from litgleaner.application.stages.base import Stage
from litgleaner.application.topics import active_topics, triage_job
from litgleaner.ports.store import JobSpec, TriageResult

TRIAGE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relevant", "reason"],
    "properties": {"relevant": {"type": "boolean"}, "reason": {"type": "string", "maxLength": 300}},
}


class TriageStage(Stage):
    name = "triage"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        for topic in active_topics(ctx).values():
            state = ctx.store.judgments.get_topic_state(topic.id)
            if state is None:
                continue
            papers = ctx.store.judgments.papers_awaiting_triage(
                topic.id, topic.topic_hash, state.triage_from
            )
            for paper_id in papers:
                yield triage_job(paper_id, topic)

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        topic = ctx.topics[payload["topic_id"]]
        paper = ctx.store.papers.get(payload["paper_id"])
        if paper is None:
            raise ValueError(f"论文不存在：{payload['paper_id']}")
        result = ctx.llm.call(
            "triage",
            {"topic_description": topic.description},
            paper_header(paper),
            TRIAGE_SCHEMA,
        )
        ctx.store.judgments.save_triage(
            TriageResult(
                paper_id=paper.id,
                topic_id=topic.id,
                topic_hash=topic.topic_hash,
                relevant=bool(result.data["relevant"]),
                reason=str(result.data["reason"]),
                prompt_version=result.prompt_version,
                model=result.model,
            ),
            ctx.now_iso(),
        )
