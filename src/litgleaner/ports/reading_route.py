"""阅读路线批量快照；缺失的评分和全文不通过旧版本补齐。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from litgleaner.domain.paper import Paper

if TYPE_CHECKING:
    from litgleaner.ports.store import FullTextState, ScoredAssessment


@dataclass(frozen=True)
class RouteSnapshot:
    papers: tuple[Paper, ...]
    scores: Mapping[str, ScoredAssessment]
    fulltexts: Mapping[str, FullTextState]
    citations: Mapping[str, int]  # 未出现的论文课题内引用数为零


class ReadingRouteRepo(Protocol):
    def snapshot(
        self, topic_id: str, topic_hash: str, semantic_hash: str, policy_hash: str
    ) -> RouteSnapshot: ...
