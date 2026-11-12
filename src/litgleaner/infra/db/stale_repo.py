"""诊断快照按表批量读取，离开短事务后转换全部历史。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, select

from litgleaner.domain.identity import Alias
from litgleaner.domain.outline import TopicOutline
from litgleaner.domain.topic_map import TopicMap
from litgleaner.infra.db.catalog_repo import library_scope
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.embedding_repo import _embedding
from litgleaner.infra.db.export_repo import _group, _reading
from litgleaner.infra.db.judgments_repo import _to_assessment
from litgleaner.infra.db.papers_repo import _to_paper
from litgleaner.infra.db.qa_repo import _answer, _question
from litgleaner.infra.db.schema import (
    assessment,
    extraction,
    fulltext,
    job,
    paper,
    paper_alias,
    paper_embedding,
    qa_message,
    reading_state,
    score,
    topic_map,
    topic_outline,
    topic_state,
    translation,
    triage,
)
from litgleaner.infra.db.translation_repo import _to_cached
from litgleaner.ports.stale import StaleAssessment, StaleSnapshot, StaleTranslation
from litgleaner.ports.store import (
    Extraction,
    FullTextState,
    ScoredAssessment,
    TopicState,
    TriageResult,
)


@dataclass(frozen=True)
class _Rows:
    papers: Sequence[Row[Any]]
    fulltexts: Sequence[Row[Any]]
    aliases: Sequence[Row[Any]]
    reading: Sequence[Row[Any]]
    extractions: Sequence[Row[Any]]
    assessments: Sequence[Row[Any]]
    scores: Sequence[Row[Any]]
    translations: Sequence[Row[Any]]
    qa: Sequence[Row[Any]]
    jobs: Sequence[Row[Any]]
    topic_states: Sequence[Row[Any]]
    triages: Sequence[Row[Any]]
    maps: Sequence[Row[Any]]
    outlines: Sequence[Row[Any]]
    embeddings: Sequence[Row[Any]]


def _scores(
    rows: Sequence[Row[Any]], assessments: tuple[StaleAssessment, ...]
) -> tuple[ScoredAssessment, ...]:
    records = {item.record.id: item.record for item in assessments}
    return tuple(
        ScoredAssessment(
            assessment=records[row.assessment_id],
            policy_hash=row.policy_hash,
            score=row.score,
            tier=row.tier,
            provisional=bool(row.provisional),
            undetermined=tuple(json.loads(row.undetermined)),
        )
        for row in rows
    )


def _ask_jobs(rows: Sequence[Row[Any]]) -> dict[str, dict[str, Any]]:
    """同问题出现多个任务时，保留按 key 升序读取的最后一个有效 payload。"""
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            payload = json.loads(row.payload)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            question_id = payload.get("question_id")
            if isinstance(question_id, str) and question_id.strip():
                result[question_id] = payload
    return result


def _fulltexts(rows: Sequence[Row[Any]]) -> dict[str, FullTextState]:
    return {
        row.paper_id: FullTextState(
            row.paper_id,
            row.status,
            row.pdf_sha256,
            row.doc_key,
            row.provider,
            row.license_basis,
            row.quality,
        )
        for row in rows
    }


def _snapshot(rows: _Rows) -> StaleSnapshot:
    aliases = _group(
        rows.aliases, lambda row: row.paper_id, lambda row: Alias(row.scheme, row.value)
    )
    assessments = tuple(
        StaleAssessment(_to_assessment(row), row.model, row.prompt_version, row.created_at)
        for row in rows.assessments
    )
    return StaleSnapshot(
        papers=tuple(_to_paper(row) for row in rows.papers),
        fulltexts=_fulltexts(rows.fulltexts),
        aliases={row.id: aliases.get(row.id, ()) for row in rows.papers},
        reading={row.paper_id: _reading(row) for row in rows.reading},
        extractions=tuple(
            Extraction(
                row.id, row.paper_id, row.input_hash, row.extractor_version, json.loads(row.data)
            )
            for row in rows.extractions
        ),
        assessments=assessments,
        scores=_scores(rows.scores, assessments),
        translations=tuple(
            StaleTranslation(_to_cached(row), row.paper_id) for row in rows.translations
        ),
        questions=tuple(_question(row) for row in rows.qa if row.role == "question"),
        answers=tuple(_answer(row) for row in rows.qa if row.role == "answer"),
        ask_jobs=_ask_jobs(rows.jobs),
        topic_states=tuple(
            TopicState(row.topic_id, row.status, row.topic_hash, row.triage_from)
            for row in rows.topic_states
        ),
        triages=tuple(
            TriageResult(
                row.paper_id,
                row.topic_id,
                row.topic_hash,
                bool(row.relevant),
                row.reason,
                row.prompt_version,
                row.model,
            )
            for row in rows.triages
        ),
        maps=tuple(TopicMap.model_validate_json(row.data) for row in rows.maps),
        outlines=tuple(TopicOutline.model_validate_json(row.data) for row in rows.outlines),
        embeddings=tuple(_embedding(row) for row in rows.embeddings),
        embedding_library_ids=frozenset(row.paper_id for row in rows.embeddings if row.in_library),
    )


def _embedding_rows(db: Database) -> Sequence[Row[Any]]:
    return db.fetch(
        select(paper_embedding, library_scope().label("in_library"))
        .join(paper, paper.c.id == paper_embedding.c.paper_id)
        .order_by(
            paper_embedding.c.paper_id,
            paper_embedding.c.created_at,
            paper_embedding.c.input_hash,
            paper_embedding.c.embedder_version,
        )
    )


class SqlStaleRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def snapshot(self) -> StaleSnapshot:
        with self._db.atomic():
            rows = _Rows(
                papers=self._db.fetch(select(paper).order_by(paper.c.id)),
                fulltexts=self._db.fetch(select(fulltext).order_by(fulltext.c.paper_id)),
                aliases=self._db.fetch(
                    select(paper_alias).order_by(
                        paper_alias.c.paper_id, paper_alias.c.scheme, paper_alias.c.value
                    )
                ),
                reading=self._db.fetch(select(reading_state).order_by(reading_state.c.paper_id)),
                extractions=self._db.fetch(select(extraction).order_by(extraction.c.id)),
                assessments=self._db.fetch(select(assessment).order_by(assessment.c.id)),
                scores=self._db.fetch(
                    select(score).order_by(score.c.assessment_id, score.c.policy_hash)
                ),
                translations=self._db.fetch(
                    select(translation).order_by(*translation.primary_key.columns)
                ),
                qa=self._db.fetch(
                    select(qa_message).order_by(qa_message.c.created_at, qa_message.c.id)
                ),
                jobs=self._db.fetch(
                    select(job.c.key, job.c.payload).where(job.c.stage == "ask").order_by(job.c.key)
                ),
                topic_states=self._db.fetch(select(topic_state).order_by(topic_state.c.topic_id)),
                triages=self._db.fetch(
                    select(triage).order_by(
                        triage.c.paper_id, triage.c.topic_id, triage.c.topic_hash
                    )
                ),
                maps=self._db.fetch(
                    select(topic_map.c.data).order_by(topic_map.c.created_at, topic_map.c.id)
                ),
                outlines=self._db.fetch(
                    select(topic_outline.c.data).order_by(
                        topic_outline.c.created_at, topic_outline.c.id
                    )
                ),
                embeddings=_embedding_rows(self._db),
            )
        return _snapshot(rows)
