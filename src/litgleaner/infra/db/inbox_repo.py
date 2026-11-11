"""用户添加的论文（第 12.8 节）。"""

from typing import Any

from sqlalchemy import Row, func, insert, select, update

from litgleaner.domain.inbox import InboxItem, InboxStatus
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import fulltext, inbox


def _to_item(row: Row[Any]) -> InboxItem:
    return InboxItem(
        id=row.id,
        kind=row.kind,
        value=row.value,
        status=row.status,
        pdf_sha256=row.pdf_sha256,
        paper_id=row.paper_id,
        error=row.error,
        created_at=row.created_at,
    )


class SqlInboxRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, item: InboxItem) -> None:
        self._db.write(
            insert(inbox).values(
                id=item.id,
                kind=item.kind,
                value=item.value,
                pdf_sha256=item.pdf_sha256,
                status=item.status,
                paper_id=item.paper_id,
                error=item.error,
                created_at=item.created_at,
                updated_at=item.created_at,
            )
        )

    def get(self, item_id: str) -> InboxItem | None:
        rows = self._db.fetch(select(inbox).where(inbox.c.id == item_id))
        return _to_item(rows[0]) if rows else None

    def finish(
        self, item_id: str, status: InboxStatus, paper_id: str | None, error: str | None, now: str
    ) -> None:
        self._db.write(
            update(inbox)
            .where(inbox.c.id == item_id)
            .values(status=status, paper_id=paper_id, error=error, updated_at=now)
        )

    def recent(self, limit: int = 50) -> tuple[InboxItem, ...]:
        rows = self._db.fetch(select(inbox).order_by(inbox.c.created_at.desc()).limit(limit))
        return tuple(_to_item(row) for row in rows)

    def ready_revision(self) -> str:
        """只观察完成的条目。计数可区分同一时刻完成的多篇论文。"""
        rows = self._db.fetch(
            select(func.count().label("n"), func.max(inbox.c.updated_at).label("latest"))
            .select_from(inbox)
            .where(inbox.c.status == "done")
        )
        return f"{rows[0].n}:{rows[0].latest}"

    def papers_without_fulltext(self) -> tuple[str, ...]:
        """用户添加、已对上论文、还没有取全文的论文。取全文不要求通过初筛。"""
        rows = self._db.fetch(
            select(inbox.c.paper_id)
            .where(
                inbox.c.status == "done",
                inbox.c.paper_id.is_not(None),
                inbox.c.paper_id.not_in(select(fulltext.c.paper_id)),
            )
            .distinct()
            .order_by(inbox.c.paper_id)
        )
        return tuple(str(row.paper_id) for row in rows)
