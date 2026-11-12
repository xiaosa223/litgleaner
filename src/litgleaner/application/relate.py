"""关系核实的预览、冻结队列、精确缓存与按论文费用作用域。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.application.relate_input import RelateInput, configuration, prepare_input
from litgleaner.application.relate_state import state_token
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper_relation import RelateBatch, RelationProposal
from litgleaner.ports.store import JobSpec


@dataclass(frozen=True)
class RelatePreview:
    paper_id: str
    plan_hash: str
    input_hash: str
    producer_version: str | None
    considered_paper_ids: tuple[str, ...]
    input_paper_ids: tuple[str, ...]
    omitted_paper_ids: tuple[str, ...]
    pairs_count: int
    input_tokens: int
    token_budget: int
    estimated_usd: float | None
    cached_batch_id: str | None
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RelateStatus:
    job_key: str
    status: str
    result: RelateBatch | None
    cost_usd: float
    last_error: str | None = None


def prepared_preview(ctx: AppContext, paper_id: str, prepared: RelateInput) -> RelatePreview:
    saved = cached(ctx, paper_id, prepared)
    route = ctx.llm.routes.get("relate")
    fingerprint = stable_hash(
        {
            "paper_id": paper_id,
            "input": prepared.input_hash,
            "producer": prepared.producer_version,
            "eligible": prepared.eligible_ids,
            "considered": prepared.considered_ids,
            "configuration": configuration(ctx),
            "price": None if route is None else route.llm.price(route.model),
        }
    )
    return RelatePreview(
        paper_id,
        fingerprint,
        prepared.input_hash,
        prepared.producer_version,
        prepared.considered_ids,
        prepared.paper_ids,
        tuple(id_ for id_ in prepared.considered_ids if id_ not in prepared.paper_ids),
        prepared.pairs_count,
        prepared.tokens,
        ctx.settings.relate.token_budget,
        0.0 if saved or not prepared.pairs_count else prepared.estimated_usd,
        saved.id if saved else None,
        prepared.warnings,
    )


def cached(ctx: AppContext, paper_id: str, prepared: RelateInput) -> RelateBatch | None:
    if prepared.producer_version is None:
        return None
    return ctx.store.relations.by_fingerprint(
        paper_id, prepared.input_hash, prepared.producer_version
    )


def preview(ctx: AppContext, paper_id: str) -> RelatePreview:
    return prepared_preview(ctx, paper_id, prepare_input(ctx, paper_id))


def queue(ctx: AppContext, paper_id: str, plan_hash: str) -> RelateStatus:
    ctx.llm.require_route("relate")
    before = state_token(ctx)
    prepared = prepare_input(ctx, paper_id)
    if prepared_preview(ctx, paper_id, prepared).plan_hash != plan_hash:
        raise InvalidRequest("关系候选、原文或配置已变化，请重新预览后确认")
    with ctx.store.atomic():
        if state_token(ctx) != before:
            raise InvalidRequest("文库读取期间发生变化，请重新预览后确认")
        return queue_prepared(ctx, paper_id, prepared)


def queue_prepared(
    ctx: AppContext, paper_id: str, prepared: RelateInput, *, scope_id: str | None = None
) -> RelateStatus:
    route = ctx.llm.require_route("relate")
    if prepared.producer_version is None:
        raise InvalidRequest("缺少关系核实生产者，请重新预览")
    key = f"relate:{paper_id}:{prepared.input_hash}:{prepared.producer_version}"
    payload = {
        "paper_id": paper_id,
        "input_hash": prepared.input_hash,
        "producer_version": prepared.producer_version,
        "inputs": prepared.text,
        "eligible_ids": prepared.eligible_ids,
        "input_paper_ids": prepared.paper_ids,
        "considered_paper_ids": prepared.considered_ids,
        "source_hashes": prepared.source_hashes,
        "configuration": dict(configuration(ctx)),
        "reading_language": ctx.settings.reading_language,
        "token_budget": ctx.settings.relate.token_budget,
        "warnings": prepared.warnings,
        "scope_id": scope_id or key,
    }
    with ctx.store.atomic():
        ctx.store.jobs.enqueue(JobSpec(key, "relate", payload), ctx.now_iso())
        if cached(ctx, paper_id, prepared) is not None:
            ctx.store.jobs.mark_done(key, ctx.now_iso())
        elif not prepared.pairs_count:
            save_batch(ctx, payload, route.model, (), ("没有可发送的完整证据对，未调用模型",))
            ctx.store.jobs.mark_done(key, ctx.now_iso())
    return status(ctx, paper_id, key)


def save_batch(
    ctx: AppContext,
    payload: Mapping[str, Any],
    model: str,
    relations: tuple[RelationProposal, ...],
    warnings: tuple[str, ...],
) -> RelateBatch:
    return ctx.store.relations.add_batch(
        RelateBatch(
            id=str(ULID()),
            paper_id=payload["paper_id"],
            input_hash=payload["input_hash"],
            producer_version=payload["producer_version"],
            model=model,
            reading_language=payload["reading_language"],
            token_budget=payload["token_budget"],
            scope_id=payload["scope_id"],
            created_at=ctx.now_iso(),
            source_hashes=dict(payload["source_hashes"]),
            considered_paper_ids=tuple(payload["considered_paper_ids"]),
            input_paper_ids=tuple(payload["input_paper_ids"]),
            inputs=payload["inputs"],
            relations=relations,
            warnings=tuple(dict.fromkeys((*payload["warnings"], *warnings))),
            configuration=dict(payload["configuration"]),
        )
    )


def status(ctx: AppContext, paper_id: str, job_key: str) -> RelateStatus:
    job = ctx.store.jobs.get(job_key)
    if job is None or job.stage != "relate" or job.payload.get("paper_id") != paper_id:
        raise NotFound("关系核实任务不存在")
    p = job.payload
    saved = ctx.store.relations.by_fingerprint(paper_id, p["input_hash"], p["producer_version"])
    scope = saved.scope_id if saved else p["scope_id"]
    return RelateStatus(
        job_key, job.status, saved, ctx.store.runs.llm_cost_for_scope(scope), job.last_error
    )
