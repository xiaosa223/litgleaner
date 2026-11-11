"""修改评价指标后的两种操作：免费重算和付费重评。"""

from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.pipeline import RunReport, run_stages
from litgleaner.application.stages.assess import assess_job, assessment_input_hash
from litgleaner.application.topics import WorkPlan, estimate
from litgleaner.domain.errors import ConfigError


@dataclass(frozen=True)
class _Pending:
    paper_id: str
    input_hash: str


def rescore(ctx: AppContext) -> RunReport:
    """用已有维度等级重算分数并更新归档，不产生 LLM 费用。"""
    return run_stages(ctx, ("score", "archive"))


def _pending_papers(ctx: AppContext, topic_id: str, since: str | None) -> tuple[_Pending, ...]:
    topic = ctx.topics[topic_id]
    semantic = ctx.rubrics[topic.rubric].semantic_hash
    pending: list[_Pending] = []
    for paper_id in ctx.store.judgments.relevant_papers(topic.id, topic.topic_hash, since):
        paper = ctx.store.papers.get(paper_id)
        state = ctx.store.papers.get_fulltext(paper_id)
        if paper is None or state is None:
            continue
        input_hash = assessment_input_hash(
            paper,
            topic,
            state,
            ctx.settings.chunker,
            reading_language=ctx.settings.reading_language,
        )
        keys = {
            (key.semantic_hash, key.topic_hash, key.input_hash)
            for key in ctx.store.judgments.assessment_keys(paper_id, topic.id)
        }
        if (semantic, topic.topic_hash, input_hash) not in keys:
            pending.append(_Pending(paper_id, input_hash))
    return tuple(pending)


def reassess(ctx: AppContext, topic_id: str, since: str | None, dry_run: bool) -> WorkPlan:
    """让已有论文按当前指标重新评估。先用 dry_run 看数量和估算费用。"""
    if topic_id not in ctx.topics:
        raise ConfigError(f"未找到主题：{topic_id}")
    pending = _pending_papers(ctx, topic_id, since)
    plan = WorkPlan(len(pending), estimate(ctx, "assess", len(pending)))
    if dry_run:
        return plan
    topic = ctx.topics[topic_id]
    rubric = ctx.rubrics[topic.rubric]
    now = ctx.now_iso()
    for item in pending:
        ctx.store.jobs.enqueue(assess_job(item.paper_id, topic, rubric, item.input_hash), now)
    return plan
