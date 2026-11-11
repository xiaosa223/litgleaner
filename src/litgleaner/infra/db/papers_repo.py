"""事实层：论文、别名、发现记录、全文状态和提取结果。"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import ColumnElement, Row, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import Paper
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import (
    artifact,
    extraction,
    fulltext,
    merge_log,
    paper,
    paper_alias,
    sighting,
    source_cursor,
)
from litgleaner.ports.store import Artifact, Extraction, FullTextState, FullTextStatus

_FILLABLE = frozenset(
    {"abstract", "venue", "issn", "published_at", "publication_types", "citation_count"}
)


def _to_paper(row: Row[Any]) -> Paper:
    return Paper(
        id=row.id,
        title=row.title,
        title_key=row.title_key,
        abstract=row.abstract,
        authors=tuple(json.loads(row.authors)),
        venue=row.venue,
        issn=row.issn,
        published_at=row.published_at,
        first_seen_at=row.first_seen_at,
        publication_types=tuple(json.loads(row.publication_types)) if row.publication_types else (),
        citation_count=row.citation_count,
    )


class SqlPaperRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def find_ids_by_aliases(self, aliases: Iterable[Alias]) -> frozenset[str]:
        found: set[str] = set()
        for alias in aliases:
            rows = self._db.fetch(
                select(paper_alias.c.paper_id).where(
                    paper_alias.c.scheme == alias.scheme, paper_alias.c.value == alias.value
                )
            )
            found.update(row.paper_id for row in rows)
        return frozenset(found)

    def find_by_title_key(self, key: str) -> tuple[Paper, ...]:
        rows = self._db.fetch(select(paper).where(paper.c.title_key == key).order_by(paper.c.id))
        return tuple(_to_paper(row) for row in rows)

    def insert(self, item: Paper) -> None:
        self._db.write(
            paper.insert().values(
                id=item.id,
                title=item.title,
                title_key=item.title_key,
                abstract=item.abstract,
                authors=json.dumps(item.authors, ensure_ascii=False),
                venue=item.venue,
                issn=item.issn,
                published_at=item.published_at,
                first_seen_at=item.first_seen_at,
                publication_types=(
                    json.dumps(item.publication_types, ensure_ascii=False)
                    if item.publication_types
                    else None
                ),
                citation_count=item.citation_count,
            )
        )

    def fill_missing(self, paper_id: str, fields: Mapping[str, str | int | None]) -> None:
        """只补空字段，不覆盖已有内容。"""
        for name, value in fields.items():
            if name not in _FILLABLE:
                raise ValueError(f"不允许补全的字段：{name}")
            if value is None:
                continue
            if name == "citation_count" and (type(value) is not int or value < 0):
                raise ValueError("citation_count 必须是非负整数")
            column = paper.c[name]
            empty: ColumnElement[bool] = column.is_(None)
            if name == "publication_types":
                if not isinstance(value, str):
                    raise ValueError("publication_types 必须是 JSON 数组")
                types = json.loads(value)
                if not isinstance(types, list) or any(
                    not isinstance(kind, str) or not kind.strip() for kind in types
                ):
                    raise ValueError("publication_types 必须是非空字符串的 JSON 数组")
                if not types:
                    continue
                empty = or_(empty, func.json_array_length(column) == 0)
            self._db.write(update(paper).where(paper.c.id == paper_id, empty).values({name: value}))

    def add_aliases(self, paper_id: str, aliases: Iterable[Alias]) -> None:
        for alias in aliases:
            self._db.write(
                insert(paper_alias)
                .values(scheme=alias.scheme, value=alias.value, paper_id=paper_id)
                .on_conflict_do_nothing()
            )

    def log_merge(
        self, paper_id: str, kind: str, status: str, detail: Mapping[str, Any], now: str
    ) -> None:
        self._db.write(
            merge_log.insert().values(
                paper_id=paper_id,
                kind=kind,
                status=status,
                detail=json.dumps(detail, ensure_ascii=False),
                created_at=now,
            )
        )

    def record_sighting(
        self, paper_id: str, source: str, query_hash: str, raw_key: str | None, now: str
    ) -> None:
        statement = insert(sighting).values(
            paper_id=paper_id,
            source=source,
            query_hash=query_hash,
            raw_key=raw_key,
            first_seen_at=now,
            last_seen_at=now,
        )
        self._db.write(
            statement.on_conflict_do_update(
                index_elements=["paper_id", "source", "query_hash"],
                set_={"last_seen_at": now, "raw_key": raw_key},
            )
        )

    def get(self, paper_id: str) -> Paper | None:
        rows = self._db.fetch(select(paper).where(paper.c.id == paper_id))
        return _to_paper(rows[0]) if rows else None

    def aliases_of(self, paper_id: str) -> tuple[Alias, ...]:
        rows = self._db.fetch(
            select(paper_alias)
            .where(paper_alias.c.paper_id == paper_id)
            .order_by(paper_alias.c.scheme, paper_alias.c.value)
        )
        return tuple(Alias(row.scheme, row.value) for row in rows)

    def get_cursor(self, source: str, query_hash: str) -> str | None:
        rows = self._db.fetch(
            select(source_cursor.c.cursor).where(
                source_cursor.c.source == source, source_cursor.c.query_hash == query_hash
            )
        )
        return rows[0].cursor if rows else None

    def set_cursor(self, source: str, query_hash: str, cursor: str | None, now: str) -> None:
        statement = insert(source_cursor).values(
            source=source, query_hash=query_hash, cursor=cursor, updated_at=now
        )
        self._db.write(
            statement.on_conflict_do_update(
                index_elements=["source", "query_hash"],
                set_={"cursor": cursor, "updated_at": now},
            )
        )

    def source_contribution(self) -> dict[str, int]:
        rows = self._db.fetch(
            select(
                sighting.c.source, func.count(sighting.c.paper_id.distinct()).label("n")
            ).group_by(sighting.c.source)
        )
        return {row.source: row.n for row in rows}

    def get_fulltext(self, paper_id: str) -> FullTextState | None:
        rows = self._db.fetch(select(fulltext).where(fulltext.c.paper_id == paper_id))
        if not rows:
            return None
        row = rows[0]
        return FullTextState(
            paper_id=row.paper_id,
            status=row.status,
            pdf_sha256=row.pdf_sha256,
            doc_key=row.doc_key,
            provider=row.provider,
            license_basis=row.license_basis,
            quality=row.quality,
        )

    def save_fulltext(self, state: FullTextState, now: str) -> None:
        values = {
            "status": state.status,
            "pdf_sha256": state.pdf_sha256,
            "doc_key": state.doc_key,
            "provider": state.provider,
            "license_basis": state.license_basis,
            "quality": state.quality,
            "updated_at": now,
        }
        self._db.write(
            insert(fulltext)
            .values(paper_id=state.paper_id, **values)
            .on_conflict_do_update(index_elements=["paper_id"], set_=values)
        )

    def papers_with_fulltext_status(self, status: FullTextStatus) -> tuple[str, ...]:
        rows = self._db.fetch(
            select(fulltext.c.paper_id)
            .where(fulltext.c.status == status)
            .order_by(fulltext.c.paper_id)
        )
        return tuple(row.paper_id for row in rows)

    def paper_with_pdf(self, sha256: str) -> str | None:
        """已经用这份 PDF 作全文的论文。"""
        rows = self._db.fetch(
            select(fulltext.c.paper_id)
            .where(fulltext.c.pdf_sha256 == sha256)
            .order_by(fulltext.c.paper_id)
            .limit(1)
        )
        return str(rows[0].paper_id) if rows else None

    def add_artifact(self, item: Artifact, now: str) -> None:
        self._db.write(
            insert(artifact)
            .values(
                sha256=item.sha256,
                kind=item.kind,
                blob_key=item.blob_key,
                size=item.size,
                producer=item.producer,
                license_basis=item.license_basis,
                pinned=False,
                created_at=now,
            )
            .on_conflict_do_nothing()
        )

    def get_extraction(self, paper_id: str) -> Extraction | None:
        rows = self._db.fetch(
            select(extraction)
            .where(extraction.c.paper_id == paper_id)
            .order_by(extraction.c.id.desc())
            .limit(1)
        )
        if not rows:
            return None
        row = rows[0]
        return Extraction(
            row.id, row.paper_id, row.input_hash, row.extractor_version, json.loads(row.data)
        )

    def insert_extraction(
        self,
        paper_id: str,
        input_hash: str,
        extractor_version: str,
        data: Mapping[str, Any],
        now: str,
    ) -> None:
        self._db.write(
            insert(extraction)
            .values(
                paper_id=paper_id,
                input_hash=input_hash,
                extractor_version=extractor_version,
                data=json.dumps(data, ensure_ascii=False, sort_keys=True),
                created_at=now,
            )
            .on_conflict_do_nothing()
        )

    def parsed_without_extraction(self) -> tuple[str, ...]:
        rows = self._db.fetch(
            select(fulltext.c.paper_id)
            .where(
                fulltext.c.status == "parsed",
                fulltext.c.paper_id.not_in(select(extraction.c.paper_id)),
            )
            .order_by(fulltext.c.paper_id)
        )
        return tuple(row.paper_id for row in rows)
