"""冻结核实凭据与追加式判断；只有状态和替代指针允许改变。"""

import json
from typing import Any

from sqlalchemy import Row, func, literal_column, or_, select, update
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper_relation import (
    PaperRelation,
    RelateBatch,
    RelationAction,
    RelationKind,
    RelationProposal,
    canonical_relation,
)
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import feedback, paper, paper_relation, relate_batch


def _relation(row: Row[Any]) -> PaperRelation:
    data = json.loads(row.data)
    return PaperRelation.model_validate(
        data | {"status": row.status, "superseded_by": row.superseded_by}
    )


def _pair(record: RelationProposal) -> tuple[str, str]:
    a, b = sorted((record.paper_a, record.paper_b))
    return a, b


class SqlRelationRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, relation_id: str) -> PaperRelation | None:
        rows = self._db.fetch(select(paper_relation).where(paper_relation.c.id == relation_id))
        return _relation(rows[0]) if rows else None

    def batch(self, batch_id: str) -> RelateBatch | None:
        rows = self._db.fetch(select(relate_batch.c.data).where(relate_batch.c.id == batch_id))
        return RelateBatch.model_validate_json(rows[0].data) if rows else None

    def by_fingerprint(
        self, paper_id: str, input_hash: str, producer_version: str
    ) -> RelateBatch | None:
        rows = self._db.fetch(
            select(relate_batch.c.data).where(
                relate_batch.c.paper_id == paper_id,
                relate_batch.c.input_hash == input_hash,
                relate_batch.c.producer_version == producer_version,
            )
        )
        return RelateBatch.model_validate_json(rows[0].data) if rows else None

    def for_paper(self, paper_id: str) -> tuple[PaperRelation, ...]:
        rows = self._db.fetch(
            select(paper_relation)
            .where(
                or_(
                    paper_relation.c.paper_a == paper_id,
                    paper_relation.c.paper_b == paper_id,
                )
            )
            .order_by(paper_relation.c.created_at.desc(), paper_relation.c.id.desc())
        )
        return tuple(_relation(row) for row in rows)

    def batches_for_paper(self, paper_id: str) -> tuple[RelateBatch, ...]:
        rows = self._db.fetch(
            select(relate_batch.c.data)
            .where(
                relate_batch.c.paper_id == paper_id,
            )
            .order_by(relate_batch.c.created_at.desc(), relate_batch.c.id.desc())
        )
        return tuple(RelateBatch.model_validate_json(row.data) for row in rows)

    def user_decided_pairs(self) -> frozenset[tuple[str, str]]:
        decisions = select(feedback.c.relation_id).where(
            feedback.c.relation_id.is_not(None),
            feedback.c.action.in_(("confirm", "reject", "change")),
        )
        rows = self._db.fetch(
            select(paper_relation.c.pair_a, paper_relation.c.pair_b)
            .where(
                or_(paper_relation.c.source == "user", paper_relation.c.id.in_(decisions)),
            )
            .distinct()
        )
        return frozenset((row.pair_a, row.pair_b) for row in rows)

    def add_batch(self, batch: RelateBatch) -> RelateBatch:
        batch = RelateBatch.model_validate_json(batch.model_dump_json())
        batch = batch.model_copy(
            update={"relations": tuple(canonical_relation(p) for p in batch.relations)}
        )
        with self._db.atomic():
            found = self.by_fingerprint(batch.paper_id, batch.input_hash, batch.producer_version)
            if found is not None:
                return found
            self._check_papers(batch)
            columns = {
                name: getattr(batch, name)
                for name in ("id", "paper_id", "input_hash", "producer_version", "created_at")
            }
            self._db.write(insert(relate_batch).values(**columns, data=batch.model_dump_json()))
            decided = self.user_decided_pairs()
            for proposal in batch.relations:
                if _pair(proposal) not in decided:
                    record = PaperRelation(
                        **proposal.model_dump(),
                        id=stable_hash({"batch": batch.id, "pair": _pair(proposal)}),
                        batch_id=batch.id,
                        input_hash=batch.input_hash,
                        producer_version=batch.producer_version,
                        created_at=batch.created_at,
                    )
                    self._insert(record)
                    self._supersede(record, models_only=True)
        return batch

    def _check_papers(self, batch: RelateBatch) -> None:
        ids = set(batch.considered_paper_ids) | {batch.paper_id}
        requested = func.json_each(json.dumps(sorted(ids))).table_valued("value")
        rows = self._db.fetch(select(paper.c.id).where(paper.c.id.in_(select(requested.c.value))))
        if {row.id for row in rows} != ids:
            raise ValueError("relate_paper_missing")

    def _insert(self, record: PaperRelation) -> None:
        columns = {
            name: getattr(record, name)
            for name in (
                "id",
                "batch_id",
                "paper_a",
                "paper_b",
                "kind",
                "source",
                "status",
                "feedback_id",
                "superseded_by",
                "created_at",
            )
        }
        pair_a, pair_b = _pair(record)
        self._db.write(
            insert(paper_relation).values(
                **columns,
                pair_a=pair_a,
                pair_b=pair_b,
                data=record.model_dump_json(),
            )
        )

    def _supersede(self, record: PaperRelation, *, models_only: bool) -> None:
        pair_a, pair_b = _pair(record)
        conditions = [
            paper_relation.c.pair_a == pair_a,
            paper_relation.c.pair_b == pair_b,
            paper_relation.c.status == "valid",
            paper_relation.c.id != record.id,
        ]
        if models_only:
            conditions.append(paper_relation.c.source == "model")
        self._db.write(
            update(paper_relation)
            .where(*conditions)
            .values(status="superseded", superseded_by=record.id)
        )

    def apply_feedback(
        self,
        relation_id: str,
        action: RelationAction,
        feedback_id: str,
        kind: RelationKind | None,
        now: str,
    ) -> PaperRelation:
        if action not in ("confirm", "reject", "change") or (action == "change") != (
            kind is not None
        ):
            raise ValueError("relation_feedback_invalid")
        with self._db.atomic():
            original = self.get(relation_id)
            if original is None:
                raise ValueError("relation_missing")
            self._check_feedback(original, action, feedback_id, kind)
            if action == "change":
                return self._change(original, feedback_id, kind, now)
            if action == "confirm":
                self._supersede(original, models_only=False)
            self._db.write(
                update(paper_relation)
                .where(paper_relation.c.id == relation_id)
                .values(
                    status="valid" if action == "confirm" else "rejected_by_user",
                    superseded_by=None,
                )
            )
            result = self.get(relation_id)
            assert result is not None
            return result

    def _check_feedback(
        self, relation: PaperRelation, action: str, feedback_id: str, kind: RelationKind | None
    ) -> None:
        rows = self._db.fetch(select(feedback).where(feedback.c.id == feedback_id))
        if (
            not rows
            or rows[0].relation_id != relation.id
            or rows[0].action != action
            or rows[0].paper_id not in _pair(relation)
        ):
            raise ValueError("relation_feedback_missing")
        payload = json.loads(rows[0].payload) if rows[0].payload else None
        if payload != {"kind": kind}:
            raise ValueError("relation_feedback_payload")
        pair_a, pair_b = _pair(relation)
        pair_ids = select(paper_relation.c.id).where(
            paper_relation.c.pair_a == pair_a, paper_relation.c.pair_b == pair_b
        )
        latest = self._db.fetch(
            select(feedback.c.id)
            .where(
                feedback.c.relation_id.in_(pair_ids),
                feedback.c.action.in_(("confirm", "reject", "change")),
            )
            .order_by(literal_column("feedback.rowid").desc())
            .limit(1)
        )
        if latest[0].id != feedback_id:
            raise ValueError("relation_feedback_not_latest")

    def _change(
        self, original: PaperRelation, feedback_id: str, kind: RelationKind | None, now: str
    ) -> PaperRelation:
        relation_id = stable_hash({"feedback": feedback_id, "relation": original.id})
        if (found := self.get(relation_id)) is not None:
            return found
        values = original.model_dump() | {
            "id": relation_id,
            "kind": kind,
            "source": "user",
            "feedback_id": feedback_id,
            "created_at": now,
            "status": "valid",
            "superseded_by": None,
        }
        changed = PaperRelation.model_validate(
            canonical_relation(PaperRelation.model_validate(values)).model_dump()
        )
        self._insert(changed)
        self._supersede(changed, models_only=False)
        self._db.write(
            update(paper_relation)
            .where(paper_relation.c.id == original.id)
            .values(
                status="superseded",
                superseded_by=changed.id,
            )
        )
        return changed
