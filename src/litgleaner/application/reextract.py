"""历史重提取的预览、指纹确认排队与只读任务状态。"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.reextract_input import (
    DocumentCache,
    PreparedExtraction,
    configuration,
    job_key,
    prepare_paper,
)
from litgleaner.application.stages.extract import extraction_input_hash, extraction_producer_version
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.hashing import stable_hash
from litgleaner.ports.reextract import ReextractSnapshot
from litgleaner.ports.store import Job, JobSpec


@dataclass(frozen=True)
class ReextractItem:
    paper_id: str
    title: str | None
    state: str
    latest_extraction_id: int | None
    cached_extraction_id: int | None
    input_hash: str | None
    producer_version: str | None
    doc_key: str | None
    job_key: str | None
    job_status: str | None
    estimated_usd: float | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ReextractPreview:
    plan_hash: str
    items: tuple[ReextractItem, ...]
    estimated_usd: float | None


@dataclass(frozen=True)
class ReextractStatus:
    job_key: str
    paper_id: str
    status: str
    result_extraction_id: int | None
    cost_usd: float
    last_error: str | None = None


def _scope(paper_ids: tuple[str, ...] | None) -> tuple[str, ...] | None:
    if paper_ids is None:
        return None
    if any(not isinstance(id_, str) or not id_.strip() for id_ in paper_ids):
        raise InvalidRequest("论文ID不能为空")
    return tuple(sorted(set(paper_ids)))


def _fingerprints(
    ctx: AppContext,
    data: ReextractSnapshot,
    paper_id: str,
) -> tuple[int | None, str | None, int | None]:
    state = data.fulltexts.get(paper_id)
    history = tuple(row for row in data.extractions if row.paper_id == paper_id)
    latest = max((row.id for row in history), default=None)
    expected = (
        extraction_input_hash(
            state.doc_key,
            ctx.settings.chunker,
            ctx.settings.reading_language,
            ctx.settings.assess_token_budget,
        )
        if state and state.doc_key
        else None
    )
    producer = extraction_producer_version(ctx)
    cached = max(
        (
            row.id
            for row in history
            if row.input_hash == expected and row.extractor_version == producer
        ),
        default=None,
    )
    return latest, expected, cached


def _prior_job(
    data: ReextractSnapshot,
    key: str | None,
    paper_id: str,
    expected: str | None,
    producer: str | None,
    *,
    cached: int | None,
) -> Job | None:
    prior = next((job for job in data.jobs if job.key == key), None)
    if prior is None and cached is not None:
        prior = next(
            (
                job
                for job in data.jobs
                if job.payload.get("paper_id") == paper_id
                and job.payload.get("input_hash") == expected
                and job.payload.get("producer_version") == producer
            ),
            None,
        )
    return prior


def _item(
    ctx: AppContext,
    data: ReextractSnapshot,
    paper_id: str,
    prepared: PreparedExtraction | None,
) -> ReextractItem:
    paper = next((row for row in data.papers if row.id == paper_id), None)
    state = data.fulltexts.get(paper_id)
    latest, expected, cached = _fingerprints(ctx, data, paper_id)
    producer = extraction_producer_version(ctx)
    warnings = tuple(
        message
        for missing, message in (
            (paper is None, "论文不存在"),
            (latest is None, "尚无历史提取，使用首次提取流程"),
            (prepared is None, "文档指针缺失或归档不可读"),
            (producer is None, "未配置提取模型"),
            (prepared is not None and not prepared.source.sources, "没有可发送的连续正文"),
        )
        if missing
    )
    item_state = (
        "blocked"
        if warnings
        else "current"
        if cached == latest and cached is not None
        else "cached_but_unselected"
        if cached is not None
        else "ready"
    )
    key = job_key(prepared) if prepared is not None else None
    prior = _prior_job(data, key, paper_id, expected, producer, cached=cached)
    return ReextractItem(
        paper_id,
        paper.title if paper else None,
        item_state,
        latest,
        cached,
        expected,
        producer,
        state.doc_key if state else None,
        prior.key if prior else key,
        prior.status if prior else None,
        None if item_state == "ready" or (latest is not None and producer is None) else 0.0,
        (*warnings, "历史缓存仅核对自然键，未保存旧正文指纹，不能完整核验旧正文")
        if cached is not None
        else warnings,
    )


def _prepare(
    ctx: AppContext,
    paper_ids: tuple[str, ...] | None,
) -> tuple[ReextractPreview, ReextractSnapshot, Mapping[str, PreparedExtraction]]:
    scope = _scope(paper_ids)
    data = ctx.store.reextract_snapshot(scope)
    ids = tuple(row.id for row in data.papers) if scope is None else scope
    documents = DocumentCache(ctx)
    prepared = {
        paper.id: value
        for paper in data.papers
        if (value := prepare_paper(ctx, data, paper, documents)) is not None
    }
    items = tuple(_item(ctx, data, id_, prepared.get(id_)) for id_ in ids)
    average = (
        ctx.store.runs.average_llm_cost("extract")
        if any(row.state == "ready" for row in items)
        else 0.0
    )
    items = tuple(
        replace(row, estimated_usd=average) if row.state == "ready" else row for row in items
    )
    total = (
        None
        if any(row.estimated_usd is None for row in items)
        else sum(row.estimated_usd or 0 for row in items)
    )
    plan = stable_hash(
        {
            "scope": scope,
            "revision": data.revision,
            "configuration": configuration(ctx),
            "manifests": {
                id_: stable_hash(prepared[id_].manifest) if id_ in prepared else None for id_ in ids
            },
        }
    )
    return ReextractPreview(plan, items, total), data, prepared


def preview(ctx: AppContext, paper_ids: tuple[str, ...] | None = None) -> ReextractPreview:
    return _prepare(ctx, paper_ids)[0]


def queue(
    ctx: AppContext,
    plan_hash: str,
    paper_ids: tuple[str, ...] | None = None,
) -> tuple[ReextractStatus, ...]:
    plan, data, prepared = _prepare(ctx, paper_ids)
    if plan.plan_hash != plan_hash:
        raise InvalidRequest("重提取范围、输入或配置已变化，请重新预览后确认")
    ctx.llm.require_route("extract")
    keys = []
    frozen_configuration = configuration(ctx)
    with ctx.store.atomic():
        if (
            ctx.store.reextract_revision(_scope(paper_ids)) != data.revision
            or configuration(ctx) != frozen_configuration
        ):
            raise InvalidRequest("确认期间重提取来源已变化，请重新预览")
        for item in plan.items:
            if item.state == "ready":
                source = prepared[item.paper_id]
                key = job_key(source)
                ctx.store.jobs.enqueue(
                    JobSpec(key, "reextract", {**source.manifest, "scope_id": key}), ctx.now_iso()
                )
                keys.append(key)
            elif item.job_status is not None and item.job_key is not None:
                keys.append(item.job_key)
    return tuple(status(ctx, key) for key in keys)


def _result_id(data: ReextractSnapshot, payload: Mapping[str, Any]) -> int | None:
    return next(
        (
            row.id
            for row in data.extractions
            if row.paper_id == payload.get("paper_id")
            and row.input_hash == payload.get("input_hash")
            and row.extractor_version == payload.get("producer_version")
        ),
        None,
    )


def status(ctx: AppContext, job_key: str) -> ReextractStatus:
    job = ctx.store.jobs.get(job_key)
    if job is None or job.stage != "reextract":
        raise NotFound("历史重提取任务不存在")
    paper_id = job.payload["paper_id"]
    data = ctx.store.reextract_snapshot((paper_id,))
    return ReextractStatus(
        job_key,
        paper_id,
        job.status,
        _result_id(data, job.payload),
        ctx.store.runs.llm_cost_for_scope(job_key),
        job.last_error,
    )
