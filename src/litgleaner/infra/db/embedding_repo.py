"""本地向量只追加；独立四查询快照不读取模型或归档正文。"""

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import Row, func, select
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.embedding import PaperEmbedding, unpack_vector
from litgleaner.infra.db.catalog_repo import library_scope
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.papers_repo import _to_paper
from litgleaner.infra.db.schema import extraction, fulltext, paper, paper_embedding
from litgleaner.ports.embedding import EmbeddingSnapshot
from litgleaner.ports.store import Extraction, FullTextState


def _embedding(row: Row[Any]) -> PaperEmbedding:
    return PaperEmbedding(
        row.paper_id,
        row.input_hash,
        row.embedder_version,
        row.configuration_version,
        row.model,
        row.dimensions,
        row.input_text,
        row.vector,
        row.source_extraction_id,
        row.created_at,
    )


@dataclass(frozen=True)
class _Rows:
    papers: Sequence[Row[Any]]
    fulltexts: Sequence[Row[Any]]
    extractions: Sequence[Row[Any]]
    embeddings: Sequence[Row[Any]]


def _snapshot(rows: _Rows) -> EmbeddingSnapshot:
    return EmbeddingSnapshot(
        papers=tuple(_to_paper(row) for row in rows.papers),
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
        extractions=tuple(
            Extraction(
                row.id, row.paper_id, row.input_hash, row.extractor_version, json.loads(row.data)
            )
            for row in rows.extractions
        ),
        embeddings=tuple(_embedding(row) for row in rows.embeddings),
    )


class SqlEmbeddingRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, record: PaperEmbedding) -> bool:
        key = (
            paper_embedding.c.paper_id == record.paper_id,
            paper_embedding.c.input_hash == record.input_hash,
            paper_embedding.c.embedder_version == record.embedder_version,
        )
        with self._db.atomic():
            if self._db.fetch(select(paper_embedding.c.paper_id).where(*key)):
                return False
            unpack_vector(record.vector, record.dimensions)
            return bool(self._db.write(insert(paper_embedding).values(**asdict(record))))

    def for_paper(self, paper_id: str) -> tuple[PaperEmbedding, ...]:
        rows = self._db.fetch(
            select(paper_embedding)
            .where(paper_embedding.c.paper_id == paper_id)
            .order_by(
                paper_embedding.c.created_at.desc(),
                paper_embedding.c.input_hash,
                paper_embedding.c.embedder_version,
            )
        )
        return tuple(_embedding(row) for row in rows)

    def snapshot(self) -> EmbeddingSnapshot:
        ids = select(paper.c.id).where(library_scope())
        latest = select(func.max(extraction.c.id)).group_by(extraction.c.paper_id)
        with self._db.atomic():
            rows = _Rows(
                papers=self._db.fetch(select(paper).where(library_scope()).order_by(paper.c.id)),
                fulltexts=self._db.fetch(
                    select(fulltext)
                    .where(fulltext.c.paper_id.in_(ids))
                    .order_by(fulltext.c.paper_id)
                ),
                extractions=self._db.fetch(
                    select(extraction)
                    .where(extraction.c.paper_id.in_(ids), extraction.c.id.in_(latest))
                    .order_by(extraction.c.paper_id)
                ),
                embeddings=self._db.fetch(
                    select(paper_embedding)
                    .where(paper_embedding.c.paper_id.in_(ids))
                    .order_by(
                        paper_embedding.c.paper_id,
                        paper_embedding.c.created_at.desc(),
                        paper_embedding.c.input_hash,
                        paper_embedding.c.embedder_version,
                    )
                ),
            )
        return _snapshot(rows)
