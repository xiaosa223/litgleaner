"""概念关系与疑似重复（第 12.7 节）：随导读由模型提议，用户在知识库里确认或删除。

不新增模型调用；关系不参与掌握程度评价。确认合并只记一条决定，概念事实不改写。
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.domain.concept_links import (
    ConceptDecision,
    ConceptLink,
    Decision,
    LinkKind,
    MergeCandidate,
    canonical_map,
    latest_decisions,
)
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.guidance import Guide


@dataclass(frozen=True)
class RelatedView:
    link_id: str
    kind: LinkKind
    direction: str  # "out"：本概念 → 对方；"in"：对方 → 本概念
    concept_id: str
    name: str
    reason: str
    confirmed: bool


@dataclass(frozen=True)
class MergedName:
    """已确认并入的概念；merge_id 用来撤销（追加一条“不是”的决定）。"""

    merge_id: str
    concept_id: str
    name: str


@dataclass(frozen=True)
class MergeView:
    merge_id: str
    from_id: str
    from_name: str
    into_id: str
    into_name: str
    reason: str


def record_from_guide(
    ctx: AppContext, guide: Guide, payload: Mapping[str, Any], data: Mapping[str, Any]
) -> None:
    """只接受输入里出现过的概念编号；模型写错、写反、自指的条目直接丢弃。"""
    paper = set(payload["concept_ids"])
    known = paper | set(payload["known_ids"])
    for item in data.get("relations", ()):
        a, b = item["concept_id"], item["related_concept_id"]
        if a in paper and b in known and a != b:
            ctx.store.concept_links.add_link(
                ConceptLink(
                    str(ULID()), a, b, item["kind"], item["reason"], guide.id, guide.created_at
                )
            )
    for item in data.get("duplicates", ()):
        a, b = item["concept_id"], item["same_as_concept_id"]
        if a in paper and b in known and a != b:
            ctx.store.concept_links.add_merge(
                MergeCandidate(str(ULID()), a, b, item["reason"], guide.id, guide.created_at)
            )


def _decisions(ctx: AppContext) -> dict[str, Decision]:
    return latest_decisions(ctx.store.concept_links.decisions())


def canonical(ctx: AppContext) -> dict[str, str]:
    """已确认合并：被并入的概念 → 保留的概念。"""
    return canonical_map(ctx.store.concept_links.merges(), _decisions(ctx))


def groups(ctx: AppContext, concept_ids: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """每个概念连同并入它的概念一起算；被并入的概念按保留的那个算。"""
    mapping = canonical(ctx)
    merged_into: dict[str, list[str]] = {}
    for source, target in mapping.items():
        merged_into.setdefault(target, []).append(source)
    result: dict[str, tuple[str, ...]] = {}
    for cid in concept_ids:
        root = mapping.get(cid, cid)
        result[cid] = (root, *sorted(merged_into.get(root, ())))
    return result


def _name(ctx: AppContext, concept_id: str) -> str:
    concept = ctx.store.knowledge.get(concept_id)
    return concept.name if concept else concept_id


def related(ctx: AppContext, concept_id: str) -> tuple[RelatedView, ...]:
    decisions = _decisions(ctx)
    members = set(groups(ctx, (concept_id,))[concept_id])
    views: list[RelatedView] = []
    for link in ctx.store.concept_links.links(members):
        if decisions.get(link.id) == "rejected":
            continue
        outgoing = link.concept_a in members
        other = link.concept_b if outgoing else link.concept_a
        if other in members:
            continue
        views.append(
            RelatedView(
                link.id,
                link.kind,
                "out" if outgoing else "in",
                other,
                _name(ctx, other),
                link.reason,
                decisions.get(link.id) == "confirmed",
            )
        )
    return tuple(views)


def pending_merges(ctx: AppContext) -> tuple[MergeView, ...]:
    decisions = _decisions(ctx)
    mapping = canonical(ctx)
    views: list[MergeView] = []
    for merge in ctx.store.concept_links.merges():
        if merge.id in decisions:
            continue
        if mapping.get(merge.concept_a, merge.concept_a) == mapping.get(
            merge.concept_b, merge.concept_b
        ):
            continue  # 已经通过别的合并归到一起
        views.append(
            MergeView(
                merge.id,
                merge.concept_a,
                _name(ctx, merge.concept_a),
                merge.concept_b,
                _name(ctx, merge.concept_b),
                merge.reason,
            )
        )
    return tuple(views)


def decide(ctx: AppContext, target: str, decision: str) -> None:
    if decision not in {"confirmed", "rejected"}:
        raise InvalidRequest("决定只能是 confirmed 或 rejected")
    known = {link.id for link in ctx.store.concept_links.links()} | {
        merge.id for merge in ctx.store.concept_links.merges()
    }
    if target not in known:
        raise NotFound("概念关系或合并提议不存在")
    ctx.store.concept_links.add_decision(
        ConceptDecision(str(ULID()), target, decision, ctx.now_iso())  # type: ignore[arg-type]
    )


def merged_into(ctx: AppContext, root: str) -> tuple[MergedName, ...]:
    decisions = _decisions(ctx)
    mapping = canonical(ctx)
    return tuple(
        MergedName(m.id, m.concept_a, _name(ctx, m.concept_a))
        for m in ctx.store.concept_links.merges()
        if decisions.get(m.id) == "confirmed" and mapping.get(m.concept_a) == root
    )
