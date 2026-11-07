"""反馈和文献库记录。反馈只追加，不修改、不删除。"""

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.sqlite import insert

from litfactory.infra.db.database import Database
from litfactory.infra.db.schema import digest, digest_item, feedback, library_entry
from litfactory.ports.store import FeedbackRecord, LibraryRecord

SAVE_ACTION = "save_to_library"


class SqlFeedbackRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, record: FeedbackRecord, now: str) -> bool:
        """同一个 external_id 只记一次。返回是否新写入。"""
        statement = (
            insert(feedback)
            .values(
                id=record.id,
                paper_id=record.paper_id,
                digest_id=record.digest_id,
                channel=record.channel,
                action=record.action,
                actor=record.actor,
                external_id=record.external_id,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=["external_id"])
        )
        return self._db.write(statement) == 1

    def _latest(self) -> Any:
        """每篇论文最后一次操作。ULID 按时间递增，同一秒内也有序。"""
        newest = (
            select(feedback.c.paper_id, func.max(feedback.c.id).label("id"))
            .group_by(feedback.c.paper_id)
            .subquery()
        )
        return (
            select(feedback.c.paper_id, feedback.c.action)
            .join(newest, feedback.c.id == newest.c.id)
            .subquery()
        )

    def pending_saves(self, library: str) -> tuple[str, ...]:
        """最后一次操作是“加入文献库”、但还没写进这个文献库的论文。"""
        latest = self._latest()
        statement = (
            select(latest.c.paper_id)
            .outerjoin(
                library_entry,
                and_(
                    library_entry.c.paper_id == latest.c.paper_id,
                    library_entry.c.library == library,
                ),
            )
            .where(latest.c.action == SAVE_ACTION, library_entry.c.paper_id.is_(None))
            .order_by(latest.c.paper_id)
        )
        return tuple(row.paper_id for row in self._db.fetch(statement))

    def latest_action(self, paper_id: str) -> str | None:
        rows = self._db.fetch(
            select(feedback.c.action)
            .where(feedback.c.paper_id == paper_id)
            .order_by(feedback.c.id.desc())
            .limit(1)
        )
        return rows[0].action if rows else None

    def save_entry(self, record: LibraryRecord, now: str) -> None:
        warnings = json.dumps(list(record.warnings), ensure_ascii=False)
        values = {
            "item_key": record.item_key,
            "created": record.created,
            "warnings": warnings if record.warnings else None,
            "saved_at": now,
        }
        statement = insert(library_entry).values(
            paper_id=record.paper_id, library=record.library, **values
        )
        self._db.write(
            statement.on_conflict_do_update(index_elements=["paper_id", "library"], set_=values)
        )

    def get_entry(self, paper_id: str, library: str) -> LibraryRecord | None:
        rows = self._db.fetch(
            select(library_entry).where(
                library_entry.c.paper_id == paper_id, library_entry.c.library == library
            )
        )
        if not rows:
            return None
        row = rows[0]
        warnings = tuple(json.loads(row.warnings)) if row.warnings else ()
        return LibraryRecord(row.paper_id, row.library, row.item_key, row.created, warnings)

    def latest_card(self, paper_id: str) -> tuple[Mapping[str, Any], str] | None:
        """这篇论文最近一次推送的卡片和主题。没推送过（比如手动加入）返回 None。"""
        rows = self._db.fetch(
            select(digest_item.c.card, digest.c.topic_id)
            .join(digest, digest.c.id == digest_item.c.digest_id)
            .where(digest_item.c.paper_id == paper_id)
            .order_by(digest.c.created_at.desc(), digest.c.id.desc())
            .limit(1)
        )
        return (json.loads(rows[0].card), rows[0].topic_id) if rows else None
