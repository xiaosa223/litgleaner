"""阅读进度和批注。批注是用户写的，可以修改；删除只做标记，便于找回。"""

import json
from typing import Any, cast

from sqlalchemy import Row, select, update
from sqlalchemy.dialects.sqlite import insert
from ulid import ULID

from litgleaner.domain.reading import (
    Annotation,
    AnnotationKind,
    BBox,
    NewAnnotation,
    ReadingState,
    ReadingStatus,
    Rect,
)
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import annotation, reading_state


def _to_annotation(row: Row[Any]) -> Annotation:
    bbox = cast(BBox, tuple(json.loads(row.bbox))) if row.bbox else None
    rects = tuple(cast(Rect, tuple(rect)) for rect in json.loads(row.rects)) if row.rects else ()
    return Annotation(
        id=row.id,
        paper_id=row.paper_id,
        kind=cast(AnnotationKind, row.kind),
        page=row.page,
        bbox=bbox,
        quote=row.quote,
        body=row.body,
        created_at=row.created_at,
        updated_at=row.updated_at,
        rects=rects,
        qa_question_id=row.qa_question_id,
    )


class SqlReaderRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get_state(self, paper_id: str) -> ReadingState | None:
        rows = self._db.fetch(select(reading_state).where(reading_state.c.paper_id == paper_id))
        if not rows:
            return None
        row = rows[0]
        position = json.loads(row.position) if row.position else None
        return ReadingState(row.paper_id, cast(ReadingStatus, row.status), position, row.updated_at)

    def set_state(
        self, paper_id: str, status: ReadingStatus, position: dict[str, Any] | None, now: str
    ) -> None:
        values = {
            "status": status,
            "position": json.dumps(position, ensure_ascii=False) if position else None,
            "updated_at": now,
        }
        self._db.write(
            insert(reading_state)
            .values(paper_id=paper_id, **values)
            .on_conflict_do_update(index_elements=[reading_state.c.paper_id], set_=values)
        )

    def papers_with_status(self, status: ReadingStatus) -> tuple[str, ...]:
        rows = self._db.fetch(
            select(reading_state.c.paper_id)
            .where(reading_state.c.status == status)
            .order_by(reading_state.c.updated_at.desc(), reading_state.c.paper_id)
        )
        return tuple(row.paper_id for row in rows)

    def add_annotation(self, item: NewAnnotation, now: str) -> Annotation:
        annotation_id = str(ULID())
        self._db.write(
            annotation.insert().values(
                id=annotation_id,
                paper_id=item.paper_id,
                kind=item.kind,
                page=item.page,
                bbox=json.dumps(list(item.bbox)) if item.bbox else None,
                quote=item.quote,
                body=item.body,
                rects=json.dumps([list(rect) for rect in item.rects]) if item.rects else None,
                created_at=now,
                updated_at=now,
                qa_question_id=item.qa_question_id,
            )
        )
        return Annotation(
            annotation_id, item.paper_id, item.kind, item.page, item.bbox,
            item.quote, item.body, now, now, item.rects, item.qa_question_id,
        )  # fmt: skip

    def annotations(self, paper_id: str) -> tuple[Annotation, ...]:
        rows = self._db.fetch(
            select(annotation)
            .where(annotation.c.paper_id == paper_id, annotation.c.deleted_at.is_(None))
            .order_by(annotation.c.id)
        )
        return tuple(_to_annotation(row) for row in rows)

    def recent_annotations(self, limit: int, offset: int) -> tuple[Annotation, ...]:
        """全部论文的笔记和高亮，最近写的在前（知识库“笔记”）。"""
        rows = self._db.fetch(
            select(annotation)
            .where(annotation.c.deleted_at.is_(None))
            .order_by(annotation.c.updated_at.desc(), annotation.c.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return tuple(_to_annotation(row) for row in rows)

    def get_annotation(self, annotation_id: str) -> Annotation | None:
        rows = self._db.fetch(
            select(annotation).where(
                annotation.c.id == annotation_id, annotation.c.deleted_at.is_(None)
            )
        )
        return _to_annotation(rows[0]) if rows else None

    def update_annotation(self, annotation_id: str, body: str | None, now: str) -> bool:
        return bool(
            self._db.write(
                update(annotation)
                .where(annotation.c.id == annotation_id, annotation.c.deleted_at.is_(None))
                .values(body=body, updated_at=now)
            )
        )

    def delete_annotation(self, annotation_id: str, now: str) -> bool:
        return bool(
            self._db.write(
                update(annotation)
                .where(annotation.c.id == annotation_id, annotation.c.deleted_at.is_(None))
                .values(deleted_at=now, updated_at=now)
            )
        )
