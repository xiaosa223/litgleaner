"""完整文库的显式回填，按稳定顺序只发送每一无序对一次。"""

from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.relate import (
    RelatePreview,
    RelateStatus,
    prepared_preview,
    queue_prepared,
)
from litgleaner.application.relate_input import (
    RelateInput,
    RelatePreparationCache,
    configuration,
    prepare_input,
)
from litgleaner.application.relate_state import state_token
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.hashing import stable_hash


@dataclass(frozen=True)
class BackfillPreview:
    plan_hash: str
    library_paper_ids: tuple[str, ...]
    plans: tuple[RelatePreview, ...]
    pairs_count: int
    pending_pairs: int
    cached_batches: int
    estimated_usd: float | None
    warnings: tuple[str, ...]


def _prepare(ctx: AppContext) -> tuple[BackfillPreview, tuple[tuple[str, RelateInput], ...], str]:
    before = state_token(ctx)
    ids = tuple(sorted(paper.id for paper in ctx.store.memory_snapshot().papers))
    cache = RelatePreparationCache()
    prepared = tuple(
        (id_, prepare_input(ctx, id_, eligible_ids=ids[:index], cache=cache))
        for index, id_ in enumerate(ids)
    )
    plans = tuple(prepared_preview(ctx, id_, item) for id_, item in prepared)
    amounts = [plan.estimated_usd for plan in plans]
    cost = (
        None if any(value is None for value in amounts) else sum(value or 0.0 for value in amounts)
    )
    fingerprint = stable_hash(
        {
            "library": ids,
            "plans": [plan.plan_hash for plan in plans],
            "configuration": configuration(ctx),
            "user_decided": sorted(ctx.store.relations.user_decided_pairs()),
            "state": before,
        }
    )
    result = BackfillPreview(
        fingerprint,
        ids,
        plans,
        sum(plan.pairs_count for plan in plans),
        sum(plan.pairs_count for plan in plans if plan.cached_batch_id is None),
        sum(plan.cached_batch_id is not None for plan in plans),
        cost,
        (
            "按论文ID稳定顺序，仅比较此前前缀；每篇最多N对，未入选和预算遗漏见各论文计划",
            "已由用户确认、删除或改类型的双端跳过；零结果与已核验缓存不重复收费",
            "费用按实际输入和每对预计输出估算；实际计费取决于模型返回",
        ),
    )
    if state_token(ctx) != before:
        raise InvalidRequest("文库读取期间发生变化，请重新预览回填")
    return result, prepared, before


def preview(ctx: AppContext) -> BackfillPreview:
    return _prepare(ctx)[0]


def queue(ctx: AppContext, plan_hash: str) -> tuple[RelateStatus, ...]:
    ctx.llm.require_route("relate")
    plan, prepared, before = _prepare(ctx)
    if plan.plan_hash != plan_hash:
        raise InvalidRequest("文库、关系反馈、候选或配置已变化，请重新预览回填")
    with ctx.store.atomic():
        if state_token(ctx) != before:
            raise InvalidRequest("文库、关系反馈、候选或配置已变化，请重新预览回填")
        return tuple(queue_prepared(ctx, id_, item) for id_, item in prepared)
