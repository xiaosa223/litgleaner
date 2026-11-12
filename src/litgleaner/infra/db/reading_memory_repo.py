"""十一组批量查询读取阅读记忆，保留全局身份及核实批次全部文档指针。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, Select, or_, select

from litgleaner.domain.paper_relation import RelateBatch
from litgleaner.infra.db.catalog_repo import library_scope
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.memory_repo import _queries as _fact_queries
from litgleaner.infra.db.memory_repo import _Rows as _FactRows
from litgleaner.infra.db.memory_repo import _snapshot as _facts
from litgleaner.infra.db.reader_repo import _to_annotation
from litgleaner.infra.db.relation_repo import _relation
from litgleaner.infra.db.schema import (
    annotation,
    feedback,
    fulltext,
    paper,
    paper_relation,
    relate_batch,
)
from litgleaner.ports.reading_memory import ReadingMemorySnapshot


@dataclass(frozen=True)
class _Rows:
    facts: _FactRows
    relations: Sequence[Row[Any]]
    batches: Sequence[Row[Any]]
    annotations: Sequence[Row[Any]]


def _snapshot(rows: _Rows) -> ReadingMemorySnapshot:
    return ReadingMemorySnapshot(
        facts=_facts(rows.facts),
        relations=tuple(_relation(row) for row in rows.relations),
        batches=tuple(RelateBatch.model_validate_json(row.data) for row in rows.batches),
        annotations=tuple(
            _to_annotation(row) for row in rows.annotations if row.body and row.body.strip()
        ),
        user_decided_relation_ids=frozenset(
            row.id for row in rows.relations if row.source == "user" or row.user_status_decided
        ),
    )


def _queries(paper_id: str) -> tuple[Select[Any], ...]:
    related = (
        paper_relation.c.status == "valid",
        or_(paper_relation.c.paper_a == paper_id, paper_relation.c.paper_b == paper_id),
    )
    decided = (
        select(feedback.c.id)
        .where(
            feedback.c.relation_id == paper_relation.c.id,
            feedback.c.action.in_(("confirm", "reject")),
        )
        .correlate(paper_relation)
        .exists()
    )
    facts = list(_fact_queries())
    facts[3] = select(fulltext).order_by(fulltext.c.paper_id)
    return (
        *facts,
        select(paper_relation, decided.label("user_status_decided"))
        .where(*related)
        .order_by(paper_relation.c.created_at, paper_relation.c.id),
        select(relate_batch)
        .where(relate_batch.c.id.in_(select(paper_relation.c.batch_id).where(*related)))
        .order_by(relate_batch.c.created_at, relate_batch.c.id),
        select(annotation)
        .where(
            annotation.c.deleted_at.is_(None),
            annotation.c.body.is_not(None),
            annotation.c.paper_id.in_(select(paper.c.id).where(library_scope())),
        )
        .order_by(annotation.c.paper_id, annotation.c.updated_at, annotation.c.id),
    )


class SqlReadingMemoryRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def snapshot(self, paper_id: str) -> ReadingMemorySnapshot:
        with self._db.atomic():
            rows = [self._db.fetch(statement) for statement in _queries(paper_id)]
        return _snapshot(_Rows(_FactRows(*rows[:8]), *rows[8:]))
