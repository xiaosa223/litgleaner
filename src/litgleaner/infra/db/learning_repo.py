"""导读、回答卡片、评价与掌握事件：只追加，同一指纹或同一卡片的重复写入返回已有记录。"""

from collections.abc import Iterable, Mapping
from typing import Any, cast

from sqlalchemy import Row, func, select
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.guidance import AnswerCard, Evaluation, Guide
from litgleaner.domain.mastery import (
    EventKind,
    MasteryEvent,
    Overall,
    QuestionKind,
    Rating,
    Verdict,
)
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import (
    answer_card,
    answer_evaluation,
    concept_occurrence,
    explanation,
    mastery_event,
    reading_state,
)

_CHUNK = 500


def _chunks(ids: Iterable[str]) -> Iterable[list[str]]:
    items = sorted(set(ids))
    for start in range(0, len(items), _CHUNK):
        yield items[start : start + _CHUNK]


def _card(row: Row[Any]) -> AnswerCard:
    return AnswerCard(
        id=row.id,
        paper_id=row.paper_id,
        explanation_id=row.explanation_id,
        question_index=row.question_index,
        revision=row.revision,
        text=row.text,
        created_at=row.created_at,
    )


def _event(row: Row[Any]) -> MasteryEvent:
    return MasteryEvent(
        row.id,
        row.concept_id,
        cast(EventKind, row.kind),
        row.level,
        cast(Rating | None, row.rating),
        row.source_id,
        row.created_at,
        cast(QuestionKind | None, row.question_kind),
        cast(Verdict | None, row.verdict),
        cast(Overall | None, row.overall),
    )


class SqlLearningRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add_guide(self, guide: Guide) -> Guide:
        with self._db.atomic():
            existing = self.guide_by_fingerprint(
                guide.paper_id, guide.fingerprint, guide.producer_version
            )
            if existing is not None:
                return existing
            self._db.write(
                insert(explanation).values(
                    id=guide.id,
                    paper_id=guide.paper_id,
                    kind="guide",
                    fingerprint=guide.fingerprint,
                    producer_version=guide.producer_version,
                    reading_language=guide.reading_language,
                    created_at=guide.created_at,
                    data=guide.model_dump_json(),
                )
            )
        return guide

    def _guides(self, *conditions: Any) -> list[Guide]:
        rows = self._db.fetch(
            select(explanation.c.data)
            .where(explanation.c.kind == "guide", *conditions)
            .order_by(explanation.c.created_at.desc(), explanation.c.id.desc())
        )
        return [Guide.model_validate_json(row.data) for row in rows]

    def guide(self, guide_id: str) -> Guide | None:
        found = self._guides(explanation.c.id == guide_id)
        return found[0] if found else None

    def guide_by_fingerprint(
        self, paper_id: str, fingerprint: str, producer_version: str
    ) -> Guide | None:
        found = self._guides(
            explanation.c.paper_id == paper_id,
            explanation.c.fingerprint == fingerprint,
            explanation.c.producer_version == producer_version,
        )
        return found[0] if found else None

    def latest_guide(self, paper_id: str) -> Guide | None:
        rows = self._db.fetch(
            select(explanation.c.data)
            .where(explanation.c.kind == "guide", explanation.c.paper_id == paper_id)
            .order_by(explanation.c.created_at.desc(), explanation.c.id.desc())
            .limit(1)
        )
        return Guide.model_validate_json(rows[0].data) if rows else None

    def guides(self, guide_ids: Iterable[str]) -> Mapping[str, Guide]:
        result: dict[str, Guide] = {}
        for chunk in _chunks(guide_ids):
            for guide in self._guides(explanation.c.id.in_(chunk)):
                result[guide.id] = guide
        return result

    def add_card(self, card: AnswerCard) -> AnswerCard:
        """修订号由仓储在同一事务里取下一个，调用方给的 revision 被忽略。"""
        with self._db.atomic():
            rows = self._db.fetch(
                select(func.max(answer_card.c.revision)).where(
                    answer_card.c.explanation_id == card.explanation_id,
                    answer_card.c.question_index == card.question_index,
                )
            )
            saved = card.model_copy(update={"revision": (rows[0][0] or 0) + 1})
            self._db.write(insert(answer_card).values(**saved.model_dump()))
        return saved

    def card(self, card_id: str) -> AnswerCard | None:
        rows = self._db.fetch(select(answer_card).where(answer_card.c.id == card_id))
        return _card(rows[0]) if rows else None

    def cards(self, explanation_id: str) -> tuple[AnswerCard, ...]:
        rows = self._db.fetch(
            select(answer_card)
            .where(answer_card.c.explanation_id == explanation_id)
            .order_by(answer_card.c.question_index, answer_card.c.revision)
        )
        return tuple(_card(row) for row in rows)

    def latest_cards(self, limit: int = 2000) -> tuple[AnswerCard, ...]:
        newest = (
            select(
                answer_card.c.explanation_id,
                answer_card.c.question_index,
                func.max(answer_card.c.revision).label("revision"),
            )
            .group_by(answer_card.c.explanation_id, answer_card.c.question_index)
            .subquery()
        )
        rows = self._db.fetch(
            select(answer_card)
            .join(
                newest,
                (answer_card.c.explanation_id == newest.c.explanation_id)
                & (answer_card.c.question_index == newest.c.question_index)
                & (answer_card.c.revision == newest.c.revision),
            )
            .order_by(answer_card.c.created_at.desc(), answer_card.c.id.desc())
            .limit(limit)
        )
        return tuple(_card(row) for row in rows)

    def add_evaluation(self, evaluation: Evaluation) -> Evaluation:
        with self._db.atomic():
            existing = self.evaluation(evaluation.card_id)
            if existing is not None:
                return existing
            self._db.write(
                insert(answer_evaluation).values(
                    id=evaluation.id,
                    card_id=evaluation.card_id,
                    model=evaluation.model,
                    prompt_version=evaluation.prompt_version,
                    input_hash=evaluation.input_hash,
                    created_at=evaluation.created_at,
                    data=evaluation.model_dump_json(),
                )
            )
        return evaluation

    def evaluation(self, card_id: str) -> Evaluation | None:
        return self.evaluations((card_id,)).get(card_id)

    def evaluations(self, card_ids: Iterable[str]) -> Mapping[str, Evaluation]:
        result: dict[str, Evaluation] = {}
        for chunk in _chunks(card_ids):
            rows = self._db.fetch(
                select(answer_evaluation.c.data).where(answer_evaluation.c.card_id.in_(chunk))
            )
            for row in rows:
                item = Evaluation.model_validate_json(row.data)
                result[item.card_id] = item
        return result

    def add_event(self, event: MasteryEvent) -> None:
        self._db.write(
            insert(mastery_event).values(
                id=event.id,
                concept_id=event.concept_id,
                kind=event.kind,
                level=event.level,
                rating=event.rating,
                question_kind=event.question_kind,
                verdict=event.verdict,
                overall=event.overall,
                source_id=event.source_id,
                created_at=event.created_at,
            )
        )

    def events(self, concept_ids: Iterable[str]) -> Mapping[str, tuple[MasteryEvent, ...]]:
        grouped: dict[str, list[MasteryEvent]] = {}
        for chunk in _chunks(concept_ids):
            rows = self._db.fetch(
                select(mastery_event)
                .where(mastery_event.c.concept_id.in_(chunk))
                .order_by(mastery_event.c.created_at, mastery_event.c.id)
            )
            for row in rows:
                grouped.setdefault(row.concept_id, []).append(_event(row))
        return {key: tuple(value) for key, value in grouped.items()}

    def concepts_with_events(self) -> tuple[str, ...]:
        rows = self._db.fetch(select(mastery_event.c.concept_id).distinct())
        return tuple(sorted(row.concept_id for row in rows))

    def seen(
        self, concept_ids: Iterable[str], exclude_paper_id: str | None = None
    ) -> frozenset[str]:
        """读过（打开过）含该概念的论文即为“见过”；可排除正在读的这篇。"""
        found: set[str] = set()
        for chunk in _chunks(concept_ids):
            rows = self._db.fetch(
                select(concept_occurrence.c.concept_id)
                .join(reading_state, reading_state.c.paper_id == concept_occurrence.c.paper_id)
                .where(
                    concept_occurrence.c.concept_id.in_(chunk),
                    concept_occurrence.c.paper_id != (exclude_paper_id or ""),
                )
                .distinct()
            )
            found.update(row.concept_id for row in rows)
        return frozenset(found)
