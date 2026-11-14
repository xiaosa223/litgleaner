"""概念关系与疑似重复：同一对概念只记一次提议；用户决定另表追加。"""

from collections.abc import Iterable
from typing import Any, cast

from sqlalchemy import Row, or_, select
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.concept_links import (
    ConceptDecision,
    ConceptLink,
    Decision,
    LinkKind,
    MergeCandidate,
)
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import concept_decision, concept_link, concept_merge_candidate


def _link(row: Row[Any]) -> ConceptLink:
    return ConceptLink(
        row.id,
        row.concept_a,
        row.concept_b,
        cast(LinkKind, row.kind),
        row.reason,
        row.guide_id,
        row.created_at,
    )


class SqlConceptLinkRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add_link(self, item: ConceptLink) -> bool:
        statement = (
            insert(concept_link)
            .values(
                id=item.id,
                concept_a=item.concept_a,
                concept_b=item.concept_b,
                kind=item.kind,
                reason=item.reason,
                guide_id=item.guide_id,
                created_at=item.created_at,
            )
            .on_conflict_do_nothing(index_elements=["concept_a", "concept_b", "kind"])
        )
        return self._db.write(statement) == 1

    def add_merge(self, item: MergeCandidate) -> bool:
        statement = (
            insert(concept_merge_candidate)
            .values(
                id=item.id,
                concept_a=item.concept_a,
                concept_b=item.concept_b,
                reason=item.reason,
                guide_id=item.guide_id,
                created_at=item.created_at,
            )
            .on_conflict_do_nothing(index_elements=["concept_a", "concept_b"])
        )
        return self._db.write(statement) == 1

    def add_decision(self, item: ConceptDecision) -> None:
        self._db.write(
            insert(concept_decision).values(
                id=item.id, target=item.target, decision=item.decision, created_at=item.created_at
            )
        )

    def links(self, concept_ids: Iterable[str] | None = None) -> tuple[ConceptLink, ...]:
        statement = select(concept_link).order_by(concept_link.c.created_at, concept_link.c.id)
        if concept_ids is not None:
            ids = sorted(set(concept_ids))
            statement = statement.where(
                or_(concept_link.c.concept_a.in_(ids), concept_link.c.concept_b.in_(ids))
            )
        return tuple(_link(row) for row in self._db.fetch(statement))

    def merges(self) -> tuple[MergeCandidate, ...]:
        rows = self._db.fetch(
            select(concept_merge_candidate).order_by(
                concept_merge_candidate.c.created_at, concept_merge_candidate.c.id
            )
        )
        return tuple(
            MergeCandidate(r.id, r.concept_a, r.concept_b, r.reason, r.guide_id, r.created_at)
            for r in rows
        )

    def decisions(self) -> tuple[ConceptDecision, ...]:
        rows = self._db.fetch(
            select(concept_decision).order_by(concept_decision.c.created_at, concept_decision.c.id)
        )
        return tuple(
            ConceptDecision(r.id, r.target, cast(Decision, r.decision), r.created_at) for r in rows
        )
