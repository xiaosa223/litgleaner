"""文库范围、课题关系与分页。全部筛选在数据库中完成。"""

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Select, and_, func, literal, or_, select, union
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Subquery

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import (
    annotation,
    digest,
    digest_item,
    inbox,
    paper,
    paper_alias,
    reading_state,
    topic_state,
    triage,
)
from litgleaner.ports.catalog import CatalogEntry, CatalogQuery, CatalogResult

_STATUSES = ("new", "queued", "reading", "read", "abandoned")


def library_scope() -> ColumnElement[bool]:
    """有阅读状态、未删除笔记，或已经对上论文的用户添加记录。"""
    return or_(
        select(reading_state.c.paper_id)
        .where(reading_state.c.paper_id == paper.c.id)
        .correlate(paper)
        .exists(),
        select(annotation.c.paper_id)
        .where(annotation.c.paper_id == paper.c.id, annotation.c.deleted_at.is_(None))
        .correlate(paper)
        .exists(),
        select(inbox.c.paper_id)
        .where(inbox.c.paper_id == paper.c.id, inbox.c.status == "done")
        .correlate(paper)
        .exists(),
    )


def _topic_links() -> Subquery:
    relevant = (
        select(triage.c.paper_id, triage.c.topic_id)
        .join(
            topic_state,
            and_(
                triage.c.topic_id == topic_state.c.topic_id,
                triage.c.topic_hash == topic_state.c.topic_hash,
            ),
        )
        .where(triage.c.relevant.is_(True))
    )
    pushed = select(digest_item.c.paper_id, digest.c.topic_id).join(
        digest, digest.c.id == digest_item.c.digest_id
    )
    return union(relevant, pushed).subquery()


def _text_filter(query: str) -> ColumnElement[bool]:
    fields = (paper.c.title, paper.c.authors, func.coalesce(paper.c.venue, ""))
    terms = []
    for word in query.casefold().split():
        alias = (
            select(paper_alias.c.paper_id)
            .where(
                paper_alias.c.paper_id == paper.c.id,
                paper_alias.c.scheme == "doi",
                func.lower(paper_alias.c.value).contains(word, autoescape=True),
            )
            .exists()
        )
        terms.append(or_(*(func.lower(f).contains(word, autoescape=True) for f in fields), alias))
    return and_(*terms) if terms else literal(True)


def _base(query: CatalogQuery, links: Subquery) -> Select[Any]:
    statement = (
        select(
            paper.c.id,
            func.coalesce(reading_state.c.status, "new").label("status"),
            func.coalesce(reading_state.c.updated_at, paper.c.first_seen_at).label("updated_at"),
        )
        .outerjoin(reading_state, reading_state.c.paper_id == paper.c.id)
        .where(library_scope(), _text_filter(query.q))
    )
    if query.topic_id is not None:
        statement = statement.where(
            select(links.c.paper_id)
            .where(links.c.paper_id == paper.c.id, links.c.topic_id == query.topic_id)
            .exists()
        )
    return statement


class SqlCatalogRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def page(self, query: CatalogQuery) -> CatalogResult:
        links = _topic_links()
        base = _base(query, links).subquery()
        counts = dict.fromkeys(("all", *_STATUSES), 0)
        for row in self._db.fetch(
            select(base.c.status, func.count().label("n")).group_by(base.c.status)
        ):
            counts[row.status] = int(row.n)
            counts["all"] += int(row.n)
        selected = select(base.c.id).order_by(base.c.updated_at.desc(), base.c.id)
        if query.status != "all":
            selected = selected.where(base.c.status == query.status)
        ids = tuple(
            row.id for row in self._db.fetch(selected.limit(query.limit).offset(query.offset))
        )
        total = counts[query.status]
        return CatalogResult(self._entries(ids, links, query.topic_id), total, counts)

    def _entries(
        self, ids: tuple[str, ...], links: Subquery, topic_id: str | None
    ) -> tuple[CatalogEntry, ...]:
        if not ids:
            return ()
        topics: dict[str, list[str]] = {paper_id: [] for paper_id in ids}
        rows = self._db.fetch(
            select(links).where(links.c.paper_id.in_(ids)).order_by(links.c.topic_id)
        )
        for row in rows:
            topics[row.paper_id].append(row.topic_id)
        cards = self._latest_cards(ids, topic_id)
        return tuple(
            CatalogEntry(
                paper_id, tuple(topics[paper_id]), *cards.get(paper_id, (None, None, None))
            )
            for paper_id in ids
        )

    def _latest_cards(
        self, ids: tuple[str, ...], topic_id: str | None
    ) -> dict[str, tuple[Mapping[str, Any], str, str]]:
        statement = (
            select(
                digest_item.c.paper_id, digest_item.c.card, digest.c.topic_id, digest.c.created_at
            )
            .join(digest, digest.c.id == digest_item.c.digest_id)
            .where(digest_item.c.paper_id.in_(ids))
            .order_by(digest.c.created_at.desc(), digest.c.id.desc(), digest_item.c.position)
        )
        if topic_id is not None:
            statement = statement.where(digest.c.topic_id == topic_id)
        found: dict[str, tuple[Mapping[str, Any], str, str]] = {}
        for row in self._db.fetch(statement):
            if row.paper_id not in found:
                found[row.paper_id] = (json.loads(row.card), row.topic_id, row.created_at)
        return found
