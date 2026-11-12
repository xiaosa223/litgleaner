"""文库事实固定批量读取，离开短事务后再转换领域对象。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, Select, func, select

from litgleaner.domain.identity import Alias
from litgleaner.domain.knowledge import normalize_concept_name
from litgleaner.infra.db.catalog_repo import library_scope
from litgleaner.infra.db.citation_repo import _citation
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.export_repo import _group, _reading
from litgleaner.infra.db.knowledge_repo import _concept, _occurrence
from litgleaner.infra.db.papers_repo import _to_paper
from litgleaner.infra.db.schema import (
    citation,
    concept,
    concept_alias,
    concept_occurrence,
    extraction,
    fulltext,
    paper,
    paper_alias,
    reading_state,
)
from litgleaner.infra.db.stale_repo import _fulltexts
from litgleaner.ports.memory import MemorySnapshot
from litgleaner.ports.store import Extraction


@dataclass(frozen=True)
class _Rows:
    papers: Sequence[Row[Any]]
    aliases: Sequence[Row[Any]]
    citations: Sequence[Row[Any]]
    fulltexts: Sequence[Row[Any]]
    extractions: Sequence[Row[Any]]
    concepts: Sequence[Row[Any]]
    occurrences: Sequence[Row[Any]]
    reading: Sequence[Row[Any]]


def _concept_names(rows: Sequence[Row[Any]]) -> dict[str, tuple[str, ...]]:
    names: dict[str, set[str]] = {}
    for row in rows:
        registered = names.setdefault(row.id, {normalize_concept_name(row.name)})
        if row.alias_normalized_name is not None:
            registered.add(row.alias_normalized_name)
    return {key: tuple(sorted(values)) for key, values in names.items()}


def _snapshot(rows: _Rows) -> MemorySnapshot:
    aliases = _group(
        rows.aliases, lambda row: row.paper_id, lambda row: Alias(row.scheme, row.value)
    )
    for row in rows.papers:
        aliases.setdefault(row.id, ())
    return MemorySnapshot(
        papers=tuple(_to_paper(row) for row in rows.papers),
        aliases=aliases,
        citations=tuple(_citation(row) for row in rows.citations),
        fulltexts=_fulltexts(rows.fulltexts),
        extractions=tuple(
            Extraction(
                row.id, row.paper_id, row.input_hash, row.extractor_version, json.loads(row.data)
            )
            for row in rows.extractions
        ),
        concepts=tuple({row.id: _concept(row) for row in rows.concepts}.values()),
        occurrences=tuple(_occurrence(row) for row in rows.occurrences),
        reading={row.paper_id: _reading(row) for row in rows.reading},
        concept_names=_concept_names(rows.concepts),
    )


def _queries() -> tuple[Select[Any], ...]:
    ids = select(paper.c.id).where(library_scope())
    latest = select(func.max(extraction.c.id)).group_by(extraction.c.paper_id)
    used_concepts = select(concept_occurrence.c.concept_id).where(
        concept_occurrence.c.paper_id.in_(ids)
    )
    return (
        select(paper).where(library_scope()).order_by(paper.c.id),
        select(paper_alias).order_by(
            paper_alias.c.paper_id, paper_alias.c.scheme, paper_alias.c.value
        ),
        select(citation)
        .where(citation.c.citing_paper_id.in_(ids))
        .order_by(
            citation.c.citing_paper_id,
            citation.c.source,
            citation.c.source_position,
            citation.c.position,
        ),
        select(fulltext).where(fulltext.c.paper_id.in_(ids)).order_by(fulltext.c.paper_id),
        select(extraction)
        .where(extraction.c.paper_id.in_(ids), extraction.c.id.in_(latest))
        .order_by(extraction.c.paper_id),
        select(concept, concept_alias.c.normalized_name.label("alias_normalized_name"))
        .outerjoin(concept_alias, concept_alias.c.concept_id == concept.c.id)
        .where(concept.c.id.in_(used_concepts))
        .order_by(concept.c.id, concept_alias.c.normalized_name),
        select(concept_occurrence)
        .where(concept_occurrence.c.paper_id.in_(ids))
        .order_by(concept_occurrence.c.paper_id, concept_occurrence.c.concept_id),
        select(reading_state)
        .where(reading_state.c.paper_id.in_(ids))
        .order_by(reading_state.c.paper_id),
    )


class SqlMemoryRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def snapshot(self) -> MemorySnapshot:
        with self._db.atomic():
            rows = _Rows(*(self._db.fetch(statement) for statement in _queries()))
        return _snapshot(rows)
