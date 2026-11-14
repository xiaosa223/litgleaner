"""概念之间的关系与疑似重复（第 12.7 节）：模型提议、附依据，用户确认或删除；不参与掌握程度评价。"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

LinkKind = Literal["prerequisite", "similar", "contrast"]
Decision = Literal["confirmed", "rejected"]
LINK_KINDS: tuple[LinkKind, ...] = ("prerequisite", "similar", "contrast")


@dataclass(frozen=True)
class ConceptLink:
    id: str
    concept_a: str  # concept_a 的关系对象是 concept_b；prerequisite 表示 b 是 a 的前置
    concept_b: str
    kind: LinkKind
    reason: str
    guide_id: str
    created_at: str


@dataclass(frozen=True)
class MergeCandidate:
    id: str
    concept_a: str  # 合并后保留 concept_b
    concept_b: str
    reason: str
    guide_id: str
    created_at: str


@dataclass(frozen=True)
class ConceptDecision:
    id: str
    target: str  # 关系或合并候选的编号
    decision: Decision
    created_at: str


def latest_decisions(decisions: Iterable[ConceptDecision]) -> dict[str, Decision]:
    """同一对象以最后一次决定为准（用户可以改主意）。"""
    result: dict[str, Decision] = {}
    for item in sorted(decisions, key=lambda d: (d.created_at, d.id)):
        result[item.target] = item.decision
    return result


def canonical_map(
    merges: Iterable[MergeCandidate], decisions: Mapping[str, Decision]
) -> dict[str, str]:
    """已确认的合并：被并入的概念 → 最终保留的概念；链式合并取到底，遇到环就停。"""
    direct = {m.concept_a: m.concept_b for m in merges if decisions.get(m.id) == "confirmed"}
    result: dict[str, str] = {}
    for start, first in direct.items():
        seen = {start}
        target = first
        while target in direct and target not in seen:
            seen.add(target)
            target = direct[target]
        if target != start:
            result[start] = target
    return result
