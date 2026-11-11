"""主题的启用、暂停和历史回填。主题定义在配置文件，状态在数据库。"""

from dataclasses import dataclass, replace

from litgleaner.application.context import AppContext
from litgleaner.domain.errors import ConfigError
from litgleaner.domain.topic import Topic
from litgleaner.ports.store import JobSpec, TopicState

ACTIVE = "active"
PAUSED = "paused"


@dataclass(frozen=True)
class WorkPlan:
    papers: int
    estimated_usd: float | None


def _require(ctx: AppContext, topic_id: str) -> Topic:
    if topic_id not in ctx.topics:
        raise ConfigError(f"未找到主题：{topic_id}（请检查 config/topics/）")
    return ctx.topics[topic_id]


def activate_topic(ctx: AppContext, topic_id: str) -> None:
    """只处理之后发现的论文。历史论文要显式 backfill，暂停期间改过定义也一样，避免意外费用。"""
    topic = _require(ctx, topic_id)
    existing = ctx.store.judgments.get_topic_state(topic_id)
    now = ctx.now_iso()
    unchanged = existing is not None and existing.topic_hash == topic.topic_hash
    start = existing.triage_from if existing and unchanged else now
    state = TopicState(topic_id, ACTIVE, topic.topic_hash, start)
    ctx.store.judgments.save_topic_state(state, now)


def pause_topic(ctx: AppContext, topic_id: str) -> None:
    topic = _require(ctx, topic_id)
    existing = ctx.store.judgments.get_topic_state(topic_id)
    now = ctx.now_iso()
    state = (
        replace(existing, status=PAUSED)
        if existing
        else TopicState(topic_id, PAUSED, topic.topic_hash, now)
    )
    ctx.store.judgments.save_topic_state(state, now)


def active_topics(ctx: AppContext) -> dict[str, Topic]:
    return {
        state.topic_id: ctx.topics[state.topic_id]
        for state in ctx.store.judgments.topic_states()
        if state.status == ACTIVE and state.topic_id in ctx.topics
    }


def sync_topics(ctx: AppContext) -> tuple[str, ...]:
    """检查配置变化。主题定义改了只影响新论文，历史论文等你决定要不要花钱重筛。"""
    warnings: list[str] = []
    now = ctx.now_iso()
    for state in ctx.store.judgments.topic_states():
        if state.status != ACTIVE:
            continue
        topic = ctx.topics.get(state.topic_id)
        if topic is None:
            warnings.append(f"启用中的主题 {state.topic_id} 已没有配置文件，已跳过")
        elif topic.topic_hash != state.topic_hash:
            updated = TopicState(state.topic_id, ACTIVE, topic.topic_hash, now)
            ctx.store.judgments.save_topic_state(updated, now)
            warnings.append(
                f"主题 {topic.id} 的定义已变化，只有新论文按新定义处理；"
                f"重筛历史请执行 topic backfill {topic.id}"
            )
    for topic in active_topics(ctx).values():
        rubric = ctx.rubrics[topic.rubric]
        ctx.store.judgments.save_rubric_snapshot(
            f"{rubric.semantic_hash}:{rubric.policy_hash}",
            rubric.id,
            rubric.model_dump_json(),
            now,
        )
    return tuple(warnings)


def estimate(ctx: AppContext, task: str, papers: int) -> float | None:
    average = ctx.store.runs.average_llm_cost(task)
    return None if average is None else average * papers


def backfill_topic(ctx: AppContext, topic_id: str, since: str | None, dry_run: bool) -> WorkPlan:
    """用库内已有元数据重新初筛。已有的 PDF、解析和提取结果都会复用。"""
    topic = _require(ctx, topic_id)
    papers = ctx.store.judgments.untriaged_papers(topic.id, topic.topic_hash, since)
    plan = WorkPlan(len(papers), estimate(ctx, "triage", len(papers)))
    if dry_run:
        return plan
    now = ctx.now_iso()
    for paper_id in papers:
        ctx.store.jobs.enqueue(triage_job(paper_id, topic), now, reopen=True)
    return plan


def triage_job(paper_id: str, topic: Topic) -> JobSpec:
    return JobSpec(
        key=f"triage:{paper_id}:{topic.id}:{topic.topic_hash}",
        stage="triage",
        payload={"paper_id": paper_id, "topic_id": topic.id, "topic_hash": topic.topic_hash},
    )
