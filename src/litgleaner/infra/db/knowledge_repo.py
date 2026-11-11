"""概念按精确归一化别名复用；别名与论文事实只追加，检索使用 FTS5。"""

import json
from typing import Any, cast

from sqlalchemy import Row, Select, func, literal, select, text
from sqlalchemy.dialects.sqlite import insert
from ulid import ULID

from litgleaner.domain.knowledge import (
    Concept,
    ConceptAlias,
    ConceptOccurrence,
    ConceptRole,
    normalize_concept_name,
)
from litgleaner.domain.reading import BBox
from litgleaner.infra.db.catalog_repo import _topic_links
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import concept, concept_alias, concept_occurrence
from litgleaner.infra.db.search_repo import _WORDS, _conditions


def _concept(row: Row[Any]) -> Concept:
    return Concept(row.id, row.name, row.created_at)


def _occurrence(row: Row[Any]) -> ConceptOccurrence:
    bbox = cast(BBox, tuple(json.loads(row.bbox))) if row.bbox is not None else None
    return ConceptOccurrence(
        row.concept_id,
        row.paper_id,
        cast(ConceptRole, row.role),
        row.definition,
        row.page,
        bbox,
        bool(row.verified),
        row.input_hash,
        row.producer_version,
        row.created_at,
    )


def _name_key(name: str) -> str:
    normalized = normalize_concept_name(name)
    if not normalized:
        raise ValueError("concept_name_empty")
    return normalized


def _filtered(topic_id: str | None, query: str) -> Select[Any]:
    statement = select(concept)
    if topic_id is not None:
        links = _topic_links()
        linked = select(links.c.paper_id).where(links.c.topic_id == topic_id)
        statement = statement.where(
            select(concept_occurrence.c.concept_id)
            .where(
                concept_occurrence.c.concept_id == concept.c.id,
                concept_occurrence.c.paper_id.in_(linked),
            )
            .exists()
        )
    if query.strip():
        terms = list(dict.fromkeys(_WORDS.findall(normalize_concept_name(query))))[:64]
        if not terms:
            return statement.where(literal(False))
        conditions, params, _ranked = _conditions(terms)
        statement = statement.where(
            text(
                "EXISTS (SELECT 1 FROM memory_fts AS f WHERE f.id=concept.id "
                "AND f.kind IN ('concept','concept_name') AND (" + conditions + "))"
            ).bindparams(**params)
        )
    return statement


class SqlKnowledgeRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, concept_id: str) -> Concept | None:
        rows = self._db.fetch(select(concept).where(concept.c.id == concept_id))
        return _concept(rows[0]) if rows else None

    def resolve(self, name: str) -> Concept | None:
        normalized = normalize_concept_name(name)
        rows = self._db.fetch(
            select(concept)
            .join(
                concept_alias,
                concept_alias.c.concept_id == concept.c.id,
            )
            .where(concept_alias.c.normalized_name == normalized)
        )
        return _concept(rows[0]) if rows else None

    def register(self, name: str, language: str | None, now: str) -> Concept:
        _name_key(name)
        with self._db.atomic():
            if existing := self.resolve(name):
                return existing
            created = Concept(str(ULID()), name, now)
            self._db.write(insert(concept).values(id=created.id, name=name, created_at=now))
            self.add_alias(created.id, name, language, now)
            return created

    def add_alias(self, concept_id: str, name: str, language: str | None, now: str) -> bool:
        normalized = _name_key(name)
        with self._db.atomic():
            rows = self._db.fetch(
                select(concept_alias.c.concept_id).where(
                    concept_alias.c.normalized_name == normalized,
                )
            )
            if rows:
                if rows[0].concept_id != concept_id:
                    raise ValueError("concept_alias_conflict")
                return False
            return bool(
                self._db.write(
                    insert(concept_alias).values(
                        normalized_name=normalized,
                        concept_id=concept_id,
                        name=name,
                        language=language,
                        created_at=now,
                    )
                )
            )

    def aliases(self, concept_id: str) -> tuple[ConceptAlias, ...]:
        rows = self._db.fetch(
            select(concept_alias)
            .where(
                concept_alias.c.concept_id == concept_id,
            )
            .order_by(concept_alias.c.normalized_name)
        )
        return tuple(
            ConceptAlias(
                row.normalized_name,
                row.concept_id,
                row.name,
                row.language,
                row.created_at,
            )
            for row in rows
        )

    def add_occurrence(self, occurrence: ConceptOccurrence) -> bool:
        return bool(
            self._db.write(
                insert(concept_occurrence)
                .values(
                    concept_id=occurrence.concept_id,
                    paper_id=occurrence.paper_id,
                    role=occurrence.role,
                    definition=occurrence.definition,
                    page=occurrence.page,
                    bbox=json.dumps(occurrence.bbox) if occurrence.bbox is not None else None,
                    verified=occurrence.verified,
                    input_hash=occurrence.input_hash,
                    producer_version=occurrence.producer_version,
                    created_at=occurrence.created_at,
                )
                .on_conflict_do_nothing(index_elements=["concept_id", "paper_id"])
            )
        )

    def occurrences(self, concept_id: str) -> tuple[ConceptOccurrence, ...]:
        rows = self._db.fetch(
            select(concept_occurrence)
            .where(
                concept_occurrence.c.concept_id == concept_id,
            )
            .order_by(concept_occurrence.c.created_at, concept_occurrence.c.paper_id)
        )
        return tuple(_occurrence(row) for row in rows)

    def for_paper(self, paper_id: str) -> tuple[ConceptOccurrence, ...]:
        rows = self._db.fetch(
            select(concept_occurrence)
            .join(
                concept,
                concept.c.id == concept_occurrence.c.concept_id,
            )
            .where(concept_occurrence.c.paper_id == paper_id)
            .order_by(concept.c.name, concept.c.id)
        )
        return tuple(_occurrence(row) for row in rows)

    def list(
        self, topic_id: str | None, query: str, limit: int, offset: int
    ) -> tuple[Concept, ...]:
        if limit <= 0:
            return ()
        statement = _filtered(topic_id, query).order_by(concept.c.name, concept.c.id)
        return tuple(
            _concept(row)
            for row in self._db.fetch(
                statement.limit(limit).offset(max(0, offset)),
            )
        )

    def count(self, topic_id: str | None, query: str) -> int:
        statement = select(func.count()).select_from(_filtered(topic_id, query).subquery())
        return int(self._db.fetch(statement)[0][0])
