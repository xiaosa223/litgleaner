"""只读关系历史和证据状态，不补算、不排队。"""

from dataclasses import dataclass, replace

from litgleaner.application.context import AppContext
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.application.stale_relations import check_relations
from litgleaner.domain.errors import NotFound
from litgleaner.domain.paper_relation import PaperRelation, RelateBatch
from litgleaner.domain.stale import StaleItem


@dataclass(frozen=True)
class RelationView:
    paper_id: str
    relations: tuple[PaperRelation, ...]
    batches: tuple[RelateBatch, ...]
    checks: tuple[StaleItem, ...]


def view(ctx: AppContext, paper_id: str) -> RelationView:
    data = ctx.store.stale_snapshot()
    if not any(paper.id == paper_id for paper in data.papers):
        raise NotFound("论文不存在")
    relations = tuple(row for row in data.relations if paper_id in (row.paper_a, row.paper_b))
    batches = tuple(row for row in data.relate_batches if row.paper_id == paper_id)
    ids = {row.id for row in relations} | {row.id for row in batches}
    needed = {row.batch_id for row in relations} | {row.id for row in batches}
    scoped = replace(
        data,
        relations=relations,
        relate_batches=tuple(row for row in data.relate_batches if row.id in needed),
    )
    checks = tuple(row for row in check_relations(StaleInputs(ctx, scoped)) if row.id in ids)
    return RelationView(paper_id, relations, batches, checks)


def batch(ctx: AppContext, paper_id: str, batch_id: str) -> RelateBatch:
    result = ctx.store.relations.batch(batch_id)
    if result is None or result.paper_id != paper_id:
        raise NotFound("核实批次不存在")
    return result
