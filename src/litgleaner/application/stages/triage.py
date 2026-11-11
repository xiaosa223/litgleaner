"""初筛阶段：便宜模型只看标题和摘要。"""

from collections.abc import Iterable, Mapping
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.documents import paper_header
from litgleaner.application.stages.base import Stage
from litgleaner.application.topics import ACTIVE, active_topics, triage_job
from litgleaner.domain.keyword_filter import KEYWORD_FILTER_VERSION, filter_keywords
from litgleaner.domain.language import language_name
from litgleaner.domain.topic import Topic
from litgleaner.ports.store import JobSpec, TriageResult

TRIAGE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relevant", "reason"],
    "properties": {"relevant": {"type": "boolean"}, "reason": {"type": "string", "maxLength": 300}},
}


def _current_topic(ctx: AppContext, payload: Mapping[str, Any]) -> Topic | None:
    topic = ctx.topics.get(payload["topic_id"])
    if topic is None:
        return None
    state = ctx.store.judgments.get_topic_state(topic.id)
    if state is None or state.status != ACTIVE or state.topic_hash != topic.topic_hash:
        return None
    planned_hash = payload.get("topic_hash")
    if planned_hash is None:
        # 旧任务的 key 已有课题指纹；只接受当前指纹的真实排队授权。
        job = ctx.store.jobs.get(triage_job(payload["paper_id"], topic).key)
        if job is None or job.status != "pending" or "topic_hash" in job.payload:
            return None
        planned_hash = topic.topic_hash
    return topic if planned_hash == topic.topic_hash else None


class TriageStage(Stage):
    name = "triage"
    # 暂停时跳过的任务没有结果，恢复后可重新计划；已有结果仍由查询排除。
    reopen_when_planned = True

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
        topic = _current_topic(ctx, payload)
        if topic is None:
            return
        paper = ctx.store.papers.get(payload["paper_id"])
        if paper is None:
            raise ValueError(f"论文不存在：{payload['paper_id']}")
        if paper.id not in ctx.store.judgments.untriaged_papers(topic.id, topic.topic_hash, None):
            return
        decision = filter_keywords(paper.title, paper.abstract, topic.query)
        relevant, reason = False, decision.reason
        prompt_version, model = KEYWORD_FILTER_VERSION, "rule"
        if decision.passed:
            result = ctx.llm.call(
                "triage",
                {
                    "topic_profile": topic.profile_text(),
                    "output_language": language_name(ctx.settings.reading_language),
                },
                paper_header(paper),
                TRIAGE_SCHEMA,
                paper_id=paper.id,
            )
            relevant, reason = bool(result.data["relevant"]), str(result.data["reason"])
            prompt_version, model = result.prompt_version, result.model
        ctx.store.judgments.save_triage(
            TriageResult(
                paper_id=paper.id,
                topic_id=topic.id,
                topic_hash=topic.topic_hash,
                relevant=relevant,
                reason=reason,
                prompt_version=prompt_version,
                model=model,
            ),
            ctx.now_iso(),
        )
