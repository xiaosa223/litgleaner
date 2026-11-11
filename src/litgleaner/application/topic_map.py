"""课题地图预览、显式排队和只读投影；新增论文不触发模型。"""

from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.survey_plan import require_topic
from litgleaner.application.topic_map_input import (
    MapInput,
    prepare_input,
    producer_version,
    source_hash,
)
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.topic import Topic
from litgleaner.domain.topic_map import TopicMap
from litgleaner.ports.store import JobSpec
from litgleaner.ports.topic_map import MapSnapshot


@dataclass(frozen=True)
class MapPreview:
    topic_id: str
    topic_hash: str
    plan_hash: str
    input_hash: str
    producer_version: str | None
    total_papers: int
    input_paper_ids: tuple[str, ...]
    omitted_paper_ids: tuple[str, ...]
    input_tokens: int
    token_budget: int
    estimated_usd: float | None
    cached_map_id: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class MapStatus:
    job_key: str
    status: str
    result: TopicMap | None
    cost_usd: float
    last_error: str | None = None


@dataclass(frozen=True)
class MapView:
    topic_id: str
    result: TopicMap | None
    total_papers: int
    new_paper_ids: tuple[str, ...]
    ungrouped: tuple[str, ...]
    stale: bool
    stale_reasons: tuple[str, ...]


def _prepare(ctx: AppContext, topic_id: str) -> tuple[MapPreview, MapInput, MapSnapshot]:
    topic = require_topic(ctx, topic_id)
    snapshot = ctx.store.topic_maps.snapshot(topic.id, topic.topic_hash)
    prepared = prepare_input(ctx, topic, snapshot)
    producer = producer_version(ctx)
    saved = (
        None
        if producer is None
        else ctx.store.topic_maps.by_fingerprint(
            topic.id, topic.topic_hash, prepared.input_hash, producer
        )
    )
    plan_hash = stable_hash(
        {
            "topic": topic.id,
            "hash": topic.topic_hash,
            "input": prepared.input_hash,
            "producer": producer,
            "considered": [paper.id for paper in snapshot.papers],
        }
    )
    cost = (
        0.0
        if saved is not None or not prepared.paper_ids
        else ctx.store.runs.average_llm_cost("map")
    )
    preview = MapPreview(
        topic.id,
        topic.topic_hash,
        plan_hash,
        prepared.input_hash,
        producer,
        len(snapshot.papers),
        prepared.paper_ids,
        prepared.omitted_ids,
        prepared.tokens,
        ctx.settings.map.token_budget,
        cost,
        saved.id if saved else None,
        prepared.warnings,
    )
    return preview, prepared, snapshot


def preview(ctx: AppContext, topic_id: str) -> MapPreview:
    return _prepare(ctx, topic_id)[0]


def queue(ctx: AppContext, topic_id: str, plan_hash: str) -> MapStatus:
    ctx.llm.require_route("map")
    plan, prepared, snapshot = _prepare(ctx, topic_id)
    if plan.plan_hash != plan_hash:
        raise InvalidRequest("地图输入或模型配置已变化，请重新预览后确认")
    if not prepared.paper_ids:
        raise InvalidRequest("没有可发送的初筛通过论文，请检查论文范围和地图输入预算")
    key = f"map:{topic_id}:{plan.topic_hash}:{plan.input_hash}:{plan.producer_version}"
    payload = {
        "topic_id": topic_id,
        "topic_hash": plan.topic_hash,
        "input_hash": plan.input_hash,
        "producer_version": plan.producer_version,
        "inputs": prepared.text,
        "input_paper_ids": prepared.paper_ids,
        "source_hashes": prepared.source_hashes,
        "considered_paper_ids": tuple(paper.id for paper in snapshot.papers),
        "reading_language": ctx.settings.reading_language,
        "token_budget": plan.token_budget,
        "warnings": prepared.warnings,
        "scope_id": key,
    }
    ctx.store.jobs.enqueue(JobSpec(key, "map", payload), ctx.now_iso())
    if plan.cached_map_id is not None:
        ctx.store.jobs.mark_done(key, ctx.now_iso())
    return status(ctx, topic_id, key)


def status(ctx: AppContext, topic_id: str, job_key: str) -> MapStatus:
    job = ctx.store.jobs.get(job_key)
    if job is None or job.stage != "map" or job.payload.get("topic_id") != topic_id:
        raise NotFound("地图任务不存在")
    p = job.payload
    saved = ctx.store.topic_maps.by_fingerprint(
        topic_id, p["topic_hash"], p["input_hash"], p["producer_version"]
    )
    scope = saved.scope_id if saved else p["scope_id"]
    return MapStatus(
        job_key, job.status, saved, ctx.store.runs.llm_cost_for_scope(scope), job.last_error
    )


def _selected_result(ctx: AppContext, topic: Topic, snapshot: MapSnapshot) -> TopicMap | None:
    producer = producer_version(ctx)
    if producer is not None:
        candidates = ctx.store.topic_maps.compatible(
            topic.id,
            topic.topic_hash,
            producer,
            ctx.settings.reading_language,
            ctx.settings.map.token_budget,
        )
        hashes = {
            paper.id: source_hash(paper, snapshot.extractions.get(paper.id))
            for paper in snapshot.papers
        }
        for saved in candidates:
            frozen = {id_: hashes[id_] for id_ in saved.input_paper_ids if id_ in hashes}
            if frozen == saved.source_hashes:
                return saved
        if candidates:
            return candidates[0]
    return ctx.store.topic_maps.latest(topic.id)


def view(ctx: AppContext, topic_id: str) -> MapView:
    topic = require_topic(ctx, topic_id)
    snapshot = ctx.store.topic_maps.snapshot(topic.id, topic.topic_hash)
    saved = _selected_result(ctx, topic, snapshot)
    ids = tuple(paper.id for paper in snapshot.papers)
    reasons: list[str] = []
    if saved:
        if saved.topic_hash != topic.topic_hash:
            reasons.append("研究画像已变化")
        if saved.reading_language != ctx.settings.reading_language:
            reasons.append("阅读语言已变化")
        if saved.token_budget != ctx.settings.map.token_budget:
            reasons.append("地图输入预算已变化")
        current_producer = producer_version(ctx)
        if current_producer is not None and current_producer != saved.producer_version:
            reasons.append("地图模型或提示词已变化")
        hashes = {
            paper.id: source_hash(paper, snapshot.extractions.get(paper.id))
            for paper in snapshot.papers
            if paper.id in saved.input_paper_ids
        }
        if hashes != saved.source_hashes:
            reasons.append("已发送论文的标题、摘要、提取或课题归属已变化")
    assigned = {id_ for group in saved.groups for id_ in group.paper_ids} if saved else set()
    considered = frozenset(saved.considered_paper_ids) if saved else frozenset()
    return MapView(
        topic_id,
        saved,
        len(ids),
        tuple(id_ for id_ in ids if id_ not in considered),
        tuple(id_ for id_ in ids if id_ not in assigned),
        bool(reasons),
        tuple(reasons),
    )
