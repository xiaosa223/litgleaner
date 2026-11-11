"""课题当前相关论文的导出快照，六次限定查询保留最新事实与判断。"""

import json
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, Select, func, select

from litgleaner.domain.identity import Alias
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.export_repo import _reading
from litgleaner.infra.db.judgments_repo import _SCORED_COLUMNS, _to_scored
from litgleaner.infra.db.papers_repo import _to_paper
from litgleaner.infra.db.schema import (
    assessment,
    extraction,
    fulltext,
    paper,
    paper_alias,
    reading_state,
    score,
    triage,
)
from litgleaner.ports.export import TopicExportQuery, TopicExportSnapshot
from litgleaner.ports.store import Extraction, FullTextState


@dataclass(frozen=True)
class _Rows:
    papers: Sequence[Row[Any]]
    aliases: Sequence[Row[Any]]
    extractions: Sequence[Row[Any]]
    scores: Sequence[Row[Any]]
    fulltexts: Sequence[Row[Any]]
    reading: Sequence[Row[Any]]


def _relevant(query: TopicExportQuery) -> Select[Any]:
    return select(triage.c.paper_id).where(
        triage.c.topic_id == query.topic_id,
        triage.c.topic_hash == query.topic_hash,
        triage.c.relevant.is_(True),
    )


def _scored(query: TopicExportQuery, candidates: Select[Any]) -> Select[Any]:
    latest = (
        select(func.max(assessment.c.id).label("id"))
        .where(
            assessment.c.paper_id.in_(candidates),
            assessment.c.topic_id == query.topic_id,
            assessment.c.topic_hash == query.topic_hash,
            assessment.c.semantic_hash == query.semantic_hash,
        )
        .group_by(assessment.c.paper_id)
        .subquery()
    )
    return (
        select(*_SCORED_COLUMNS)
        .select_from(assessment)
        .join(latest, assessment.c.id == latest.c.id)
        .join(score, score.c.assessment_id == assessment.c.id)
        .where(score.c.policy_hash == query.policy_hash)
        .order_by(assessment.c.paper_id)
    )


def _snapshot(rows: _Rows) -> TopicExportSnapshot:
    aliases: dict[str, list[Alias]] = defaultdict(list)
    for row in rows.aliases:
        aliases[row.paper_id].append(Alias(row.scheme, row.value))
    return TopicExportSnapshot(
        papers=tuple(_to_paper(row) for row in rows.papers),
        aliases={row.id: tuple(aliases[row.id]) for row in rows.papers},
        extractions={
            row.paper_id: Extraction(
                row.id, row.paper_id, row.input_hash, row.extractor_version, json.loads(row.data)
            )
            for row in rows.extractions
        },
        scores={row.paper_id: _to_scored(row) for row in rows.scores},
        fulltexts={
            row.paper_id: FullTextState(
                row.paper_id,
                row.status,
                row.pdf_sha256,
                row.doc_key,
                row.provider,
                row.license_basis,
                row.quality,
            )
            for row in rows.fulltexts
        },
        reading={row.paper_id: _reading(row) for row in rows.reading},
    )


class SqlTopicExportRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def snapshot(self, query: TopicExportQuery) -> TopicExportSnapshot:
        candidates = _relevant(query)
        latest_extractions = (
            select(func.max(extraction.c.id))
            .where(extraction.c.paper_id.in_(candidates))
            .group_by(extraction.c.paper_id)
        )
        with self._db.atomic():
            rows = _Rows(
                papers=self._db.fetch(
                    select(paper).where(paper.c.id.in_(candidates)).order_by(paper.c.id)
                ),
                aliases=self._db.fetch(
                    select(paper_alias)
                    .where(paper_alias.c.paper_id.in_(candidates))
                    .order_by(paper_alias.c.paper_id, paper_alias.c.scheme, paper_alias.c.value)
                ),
                extractions=self._db.fetch(
                    select(extraction)
                    .where(extraction.c.id.in_(latest_extractions))
                    .order_by(extraction.c.paper_id)
                ),
                scores=self._db.fetch(_scored(query, candidates)),
                fulltexts=self._db.fetch(
                    select(fulltext)
                    .where(fulltext.c.paper_id.in_(candidates))
                    .order_by(fulltext.c.paper_id)
                ),
                reading=self._db.fetch(
                    select(reading_state)
                    .where(reading_state.c.paper_id.in_(candidates))
                    .order_by(reading_state.c.paper_id)
                ),
            )
        return _snapshot(rows)
