"""起草检索式（第 7.2 节）：只读预览 → 携计划指纹确认排队 → 按需阶段调用一次模型。

草稿写在主题文件旁边，不会自动生效；研究者确认后由 TopicSettings.apply_query 写入并启用。
"""

import json
from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.survey_plan import require_topic
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.glossary import GlossaryTerm
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.identity import Alias, normalize_doi
from litgleaner.domain.topic import Topic, TopicQuery
from litgleaner.domain.topic_draft import TopicDraft, profile_hash
from litgleaner.ports.store import JobSpec

STAGE = "draft"
MAX_SEEDS = 10
ABSTRACT_CHARS = 1500
TITLE_CHARS = 300


@dataclass(frozen=True)
class DraftPreview:
    topic_id: str
    plan_hash: str
    estimated_usd: float | None
    cached: bool
    seed_titles: tuple[str, ...]
    seeds_missing: tuple[str, ...]  # 种子 DOI 还不在文库里，这次不会发给模型
    current: TopicQuery


@dataclass(frozen=True)
class DraftStatus:
    job_key: str
    status: str
    draft: TopicDraft | None
    cost_usd: float
    last_error: str | None = None


@dataclass(frozen=True)
class DraftView:
    topic_id: str
    draft: TopicDraft | None
    stale: bool  # 草稿依据的背景、内容、目标或阅读语言已经改了
    current: TopicQuery
    glossary: tuple[GlossaryTerm, ...]


@dataclass(frozen=True)
class _Plan:
    preview: DraftPreview
    profile_hash: str
    producer: str | None
    inputs: str


def producer_version(ctx: AppContext) -> str | None:
    route = ctx.llm.routes.get(STAGE)
    if route is None:
        return None
    return stable_hash(
        {
            "version": "topic-draft-v1",
            "model": route.model,
            "prompt": route.prompt.version,
            "provider": route.provider_version,
        }
    )


def _seeds(ctx: AppContext, topic: Topic) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    """只用文库里已有的种子论文（不访问网络）；查不到的列出来，由界面提示。"""
    found: list[dict[str, str]] = []
    missing: list[str] = []
    for value in topic.seeds.dois[:MAX_SEEDS]:
        doi = normalize_doi(value) or value
        ids = ctx.store.papers.find_ids_by_aliases((Alias("doi", doi),))
        paper = ctx.store.papers.get(next(iter(ids))) if len(ids) == 1 else None
        if paper is None:
            missing.append(value)
        else:
            abstract = (paper.abstract or "")[:ABSTRACT_CHARS]
            found.append({"title": paper.title[:TITLE_CHARS], "abstract": abstract})
    return found, tuple(missing)


def _plan(ctx: AppContext, topic_id: str) -> _Plan:
    topic = require_topic(ctx, topic_id)
    seeds, missing = _seeds(ctx, topic)
    inputs = json.dumps(
        {
            "profile": topic.profile_text(),
            "current_query": topic.query.model_dump(mode="json"),
            "seed_papers": seeds,
        },
        ensure_ascii=False,
    )
    producer = producer_version(ctx)
    basis = profile_hash(topic)
    plan_hash = stable_hash(
        {
            "topic": topic.id,
            "profile": basis,
            "inputs": inputs,
            "producer": producer,
            "language": ctx.settings.reading_language,
        }
    )
    saved = ctx.topic_drafts.load(topic.id) if ctx.topic_drafts else None
    cached = saved is not None and saved.plan_hash == plan_hash
    cost = 0.0 if cached else ctx.store.runs.average_llm_cost(STAGE)
    preview = DraftPreview(
        topic.id, plan_hash, cost, cached, tuple(s["title"] for s in seeds), missing, topic.query
    )
    return _Plan(preview, basis, producer, inputs)


def preview(ctx: AppContext, topic_id: str) -> DraftPreview:
    return _plan(ctx, topic_id).preview


def current_plan_hash(ctx: AppContext, topic_id: str) -> str:
    """执行前重新计算：确认后描述、检索式、种子、模型或阅读语言变了，旧计划就作废。"""
    return _plan(ctx, topic_id).preview.plan_hash


def queue(ctx: AppContext, topic_id: str, plan_hash: str) -> DraftStatus:
    ctx.llm.require_route(STAGE)
    plan = _plan(ctx, topic_id)
    if plan.preview.plan_hash != plan_hash:
        raise InvalidRequest("课题描述或模型配置已变化，请重新预览后确认")
    key = f"draft:{topic_id}:{plan_hash}"
    payload = {
        "topic_id": topic_id,
        "plan_hash": plan_hash,
        "profile_hash": plan.profile_hash,
        "producer_version": plan.producer,
        "reading_language": ctx.settings.reading_language,
        "inputs": plan.inputs,
        "seed_titles": plan.preview.seed_titles,
        "scope_id": key,
    }
    now = ctx.now_iso()
    existing = ctx.store.jobs.get(key)
    ctx.store.jobs.enqueue(JobSpec(key, STAGE, payload), now)
    if plan.preview.cached:
        ctx.store.jobs.mark_done(key, now)
    elif existing is not None and existing.status != "pending":
        ctx.store.jobs.reopen(key, now)  # 上次失败或草稿文件被删：同一计划再次确认才重做
    return status(ctx, topic_id, key)


def status(ctx: AppContext, topic_id: str, job_key: str) -> DraftStatus:
    job = ctx.store.jobs.get(job_key)
    if job is None or job.stage != STAGE or job.payload.get("topic_id") != topic_id:
        raise NotFound("起草任务不存在")
    saved = ctx.topic_drafts.load(topic_id) if ctx.topic_drafts else None
    draft = saved if saved and saved.plan_hash == job.payload["plan_hash"] else None
    cost = ctx.store.runs.llm_cost_for_scope(job.payload["scope_id"])
    return DraftStatus(job_key, job.status, draft, cost, job.last_error)


def view(ctx: AppContext, topic_id: str) -> DraftView:
    topic = require_topic(ctx, topic_id)
    draft = ctx.topic_drafts.load(topic.id) if ctx.topic_drafts else None
    stale = draft is not None and (
        draft.profile_hash != profile_hash(topic)
        or draft.reading_language != ctx.settings.reading_language
    )
    glossary = ctx.glossaries.get(topic.id)
    return DraftView(topic.id, draft, stale, topic.query, glossary.terms if glossary else ())
