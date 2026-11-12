"""首次入库、核实缓存及关系历史的持久化接口。"""

from collections.abc import Mapping
from typing import Any, Protocol

from litgleaner.domain.paper_relation import (
    MemoryAdmission,
    PaperRelation,
    RelateBatch,
    RelationAction,
    RelationKind,
)


class AdmissionRepo(Protocol):
    def get(self, paper_id: str) -> MemoryAdmission | None: ...
    def add(self, paper_id: str, configuration: Mapping[str, Any], now: str) -> MemoryAdmission: ...
    def before(self, admission_id: int) -> tuple[str, ...]: ...


class RelationRepo(Protocol):
    def get(self, relation_id: str) -> PaperRelation | None: ...
    def by_fingerprint(
        self, paper_id: str, input_hash: str, producer_version: str
    ) -> RelateBatch | None: ...
    def add_batch(self, batch: RelateBatch) -> RelateBatch: ...
    def batch(self, batch_id: str) -> RelateBatch | None: ...
    def for_paper(self, paper_id: str) -> tuple[PaperRelation, ...]: ...
    def batches_for_paper(self, paper_id: str) -> tuple[RelateBatch, ...]: ...
    def user_decided_pairs(self) -> frozenset[tuple[str, str]]: ...
    def apply_feedback(
        self,
        relation_id: str,
        action: RelationAction,
        feedback_id: str,
        kind: RelationKind | None,
        now: str,
    ) -> PaperRelation: ...
