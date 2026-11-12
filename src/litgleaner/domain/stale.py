"""派生结果的只读核验；已知失配优先，缺失证据不能当作核验通过。"""

from dataclasses import dataclass
from typing import Literal

StaleKind = Literal[
    "extraction",
    "assessment",
    "score",
    "translation",
    "answer",
    "map",
    "outline",
    "embedding",
    "relation",
    "relate_batch",
]
StaleStatus = Literal["current", "stale", "unknown"]


@dataclass(frozen=True)
class StaleItem:
    kind: StaleKind
    id: str
    paper_id: str | None
    topic_id: str | None
    status: StaleStatus
    reasons: tuple[str, ...]
    unchecked: tuple[str, ...]
    selected: bool | None


def item(
    kind: StaleKind,
    id: str,
    paper_id: str | None,
    topic_id: str | None,
    *,
    reasons: tuple[str, ...] | list[str] = (),
    unchecked: tuple[str, ...] | list[str] = (),
    selected: bool | None = None,
) -> StaleItem:
    failed, missing = tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(unchecked))
    status: StaleStatus = "stale" if failed else "unknown" if missing else "current"
    return StaleItem(kind, id, paper_id, topic_id, status, failed, missing, selected)


@dataclass(frozen=True)
class StaleReport:
    items: tuple[StaleItem, ...]
    current: int
    stale: int
    unknown: int

    @property
    def total(self) -> int:
        return len(self.items)
