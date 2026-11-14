"""全库固定批量查询；SQL 事务结束后再组装导出内容。"""

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import Row, select, text

from litgleaner.domain.correction import Correction
from litgleaner.domain.guidance import Evaluation, Guide
from litgleaner.domain.identity import Alias
from litgleaner.domain.knowledge import ConceptAlias
from litgleaner.domain.reading import ReadingState, ReadingStatus
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.knowledge_repo import _concept, _occurrence
from litgleaner.infra.db.learning_repo import _card, _event
from litgleaner.infra.db.papers_repo import _to_paper
from litgleaner.infra.db.qa_repo import _answer, _question
from litgleaner.infra.db.reader_repo import _to_annotation
from litgleaner.infra.db.schema import (
    annotation,
    answer_card,
    answer_evaluation,
    assessment,
    concept,
    concept_alias,
    concept_occurrence,
    explanation,
    extraction,
    fulltext,
    mastery_event,
    paper,
    paper_alias,
    paper_correction,
    qa_message,
    reading_state,
    score,
)
from litgleaner.ports.export import ExportConcept, ExportLearning, ExportPaper, ExportSnapshot


@dataclass(frozen=True)
class _Rows:
    papers: Sequence[Row[Any]]
    aliases: Sequence[Row[Any]]
    readings: Sequence[Row[Any]]
    annotations: Sequence[Row[Any]]
    extractions: Sequence[Row[Any]]
    assessments: Sequence[Row[Any]]
    scores: Sequence[Row[Any]]
    questions_and_answers: Sequence[Row[Any]]
    fulltexts: Sequence[Row[Any]]
    concepts: Sequence[Row[Any]]
    concept_aliases: Sequence[Row[Any]]
    occurrences: Sequence[Row[Any]]
    guides: Sequence[Row[Any]] = ()
    cards: Sequence[Row[Any]] = ()
    evaluations: Sequence[Row[Any]] = ()
    events: Sequence[Row[Any]] = ()
    corrections: Sequence[Row[Any]] = ()


def _learning(rows: _Rows) -> ExportLearning:
    return ExportLearning(
        tuple(Guide.model_validate_json(row.data) for row in rows.guides if row.kind == "guide"),
        tuple(_card(row) for row in rows.cards),
        tuple(Evaluation.model_validate_json(row.data) for row in rows.evaluations),
        tuple(_event(row) for row in rows.events),
        tuple(
            Correction(r.id, r.paper_id, r.field, r.value, r.created_at) for r in rows.corrections
        ),
    )


def _group[K, T](
    rows: Iterable[Row[Any]], key: Callable[[Row[Any]], K], convert: Callable[[Row[Any]], T]
) -> dict[K, tuple[T, ...]]:
    groups: dict[K, list[T]] = {}
    for row in rows:
        groups.setdefault(key(row), []).append(convert(row))
    return {key: tuple(items) for key, items in groups.items()}


def _reading(row: Row[Any]) -> ReadingState:
    position = json.loads(row.position) if row.position is not None else None
    return ReadingState(row.paper_id, cast(ReadingStatus, row.status), position, row.updated_at)


def _json_record(row: Row[Any], *json_fields: str) -> dict[str, Any]:
    record = dict(row._mapping)
    for name in json_fields:
        record[name] = json.loads(record[name])
    return record


def _assessment(row: Row[Any], scores: Mapping[int, tuple[dict[str, Any], ...]]) -> dict[str, Any]:
    record = _json_record(row, "levels", "evidence", "goal_links")
    record["scores"] = list(scores.get(row.id, ()))
    return record


def _papers(rows: _Rows) -> tuple[ExportPaper, ...]:
    aliases = _group(
        rows.aliases, lambda row: row.paper_id, lambda row: Alias(row.scheme, row.value)
    )
    readings = {row.paper_id: _reading(row) for row in rows.readings}
    annotations = _group(rows.annotations, lambda row: row.paper_id, _to_annotation)
    extractions = _group(
        rows.extractions, lambda row: row.paper_id, lambda row: _json_record(row, "data")
    )
    scores = _group(
        rows.scores, lambda row: row.assessment_id, lambda row: _json_record(row, "undetermined")
    )
    assessments = _group(
        rows.assessments, lambda row: row.paper_id, lambda row: _assessment(row, scores)
    )
    questions = _group(
        (row for row in rows.questions_and_answers if row.role == "question"),
        lambda row: row.paper_id,
        _question,
    )
    answers = _group(
        (row for row in rows.questions_and_answers if row.role == "answer"),
        lambda row: row.paper_id,
        _answer,
    )
    pdfs = {row.paper_id: row.pdf_sha256 for row in rows.fulltexts}
    return tuple(
        ExportPaper(
            paper=_to_paper(row),
            aliases=aliases.get(row.id, ()),
            reading=readings.get(row.id),
            annotations=annotations.get(row.id, ()),
            extractions=extractions.get(row.id, ()),
            assessments=assessments.get(row.id, ()),
            questions=questions.get(row.id, ()),
            answers=answers.get(row.id, ()),
            pdf_sha256=pdfs.get(row.id),
        )
        for row in rows.papers
    )


def _concepts(rows: _Rows) -> tuple[ExportConcept, ...]:
    aliases = _group(
        rows.concept_aliases,
        lambda row: row.concept_id,
        lambda row: ConceptAlias(
            row.normalized_name, row.concept_id, row.name, row.language, row.created_at
        ),
    )
    occurrences = _group(rows.occurrences, lambda row: row.concept_id, _occurrence)
    return tuple(
        ExportConcept(_concept(row), aliases.get(row.id, ()), occurrences.get(row.id, ()))
        for row in rows.concepts
    )


class SqlExportRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def snapshot(self) -> ExportSnapshot:
        with self._db.atomic():
            rows = _Rows(
                papers=self._db.fetch(select(paper).order_by(paper.c.id)),
                aliases=self._db.fetch(
                    select(paper_alias).order_by(
                        paper_alias.c.paper_id, paper_alias.c.scheme, paper_alias.c.value
                    )
                ),
                readings=self._db.fetch(select(reading_state).order_by(reading_state.c.paper_id)),
                annotations=self._db.fetch(
                    select(annotation)
                    .where(annotation.c.deleted_at.is_(None))
                    .order_by(annotation.c.paper_id, annotation.c.id)
                ),
                extractions=self._db.fetch(
                    select(extraction).order_by(extraction.c.paper_id, extraction.c.id)
                ),
                assessments=self._db.fetch(
                    select(assessment).order_by(assessment.c.paper_id, assessment.c.id)
                ),
                scores=self._db.fetch(
                    select(score).order_by(score.c.assessment_id, score.c.policy_hash)
                ),
                questions_and_answers=self._db.fetch(
                    select(qa_message).order_by(
                        qa_message.c.paper_id, qa_message.c.created_at, qa_message.c.id
                    )
                ),
                fulltexts=self._db.fetch(select(fulltext).order_by(fulltext.c.paper_id)),
                concepts=self._db.fetch(select(concept).order_by(concept.c.id)),
                concept_aliases=self._db.fetch(
                    select(concept_alias).order_by(
                        concept_alias.c.concept_id, concept_alias.c.normalized_name
                    )
                ),
                occurrences=self._db.fetch(
                    select(concept_occurrence).order_by(
                        concept_occurrence.c.concept_id,
                        concept_occurrence.c.created_at,
                        concept_occurrence.c.paper_id,
                    )
                ),
                **self._learning_rows(),
            )
        return ExportSnapshot(_papers(rows), _concepts(rows), _learning(rows))

    def _learning_rows(self) -> dict[str, Sequence[Row[Any]]]:
        """学习记录的 4 次无参查询；只在 snapshot 的同一事务里调用。"""
        return {
            "guides": self._db.fetch(
                select(explanation.c.kind, explanation.c.data)
                .where(text("explanation.kind = 'guide'"))  # 字面量，保持无参查询
                .order_by(explanation.c.created_at, explanation.c.id)
            ),
            "cards": self._db.fetch(
                select(answer_card).order_by(
                    answer_card.c.explanation_id,
                    answer_card.c.question_index,
                    answer_card.c.revision,
                )
            ),
            "evaluations": self._db.fetch(
                select(answer_evaluation.c.data).order_by(answer_evaluation.c.card_id)
            ),
            "corrections": self._db.fetch(
                select(paper_correction).order_by(
                    paper_correction.c.paper_id,
                    paper_correction.c.created_at,
                    paper_correction.c.id,
                )
            ),
            "events": self._db.fetch(
                select(mastery_event).order_by(
                    mastery_event.c.concept_id, mastery_event.c.created_at, mastery_event.c.id
                )
            ),
        }
