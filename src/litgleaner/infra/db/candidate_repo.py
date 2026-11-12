"""候选独立十查询一致快照；已有文本索引只读，不补算或访问模型。"""

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import (
    Row,
    Select,
    String,
    column,
    false,
    func,
    literal,
    literal_column,
    or_,
    select,
    table,
)
from sqlalchemy import (
    cast as sql_cast,
)

from litgleaner.domain.memory_terms import candidate_terms
from litgleaner.infra.db.catalog_repo import library_scope
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.embedding_repo import _embedding
from litgleaner.infra.db.memory_repo import _queries, _Rows, _snapshot
from litgleaner.infra.db.schema import extraction, fulltext, paper, paper_embedding
from litgleaner.ports.memory_candidates import (
    CandidateSnapshot,
    CandidateTextHit,
    CandidateTextKind,
)

_TEXT = table(
    "memory_fts",
    *(column(name) for name in ("kind", "id", "paper_id", "page", "source_version")),
).alias("f")


def _current_text() -> tuple[Any, ...]:
    document = (
        select(fulltext.c.paper_id)
        .where(
            fulltext.c.paper_id == _TEXT.c.paper_id, fulltext.c.doc_key == _TEXT.c.source_version
        )
        .exists()
    )
    latest = (
        select(func.max(extraction.c.id))
        .where(extraction.c.paper_id == _TEXT.c.paper_id)
        .correlate(_TEXT)
        .scalar_subquery()
    )
    return (
        or_(_TEXT.c.kind != "paper", _TEXT.c.id == _TEXT.c.paper_id),
        or_(_TEXT.c.kind != "fulltext", document),
        or_(_TEXT.c.kind != "extraction", _TEXT.c.id == sql_cast(latest, String)),
    )


def _text_query(paper_id: str, terms: tuple[str, ...]) -> Select[Any]:
    if not terms:
        return select(literal(None)).where(false())
    rank = func.bm25(literal_column("memory_fts"), 0, 0, 0, 0, 0, 0, 4, 1).label("rank")
    match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
    return (
        select(_TEXT, rank)
        .select_from(_TEXT.join(paper, _TEXT.c.paper_id == paper.c.id))
        .where(
            library_scope(),
            _TEXT.c.paper_id != paper_id,
            _TEXT.c.kind.in_(("paper", "fulltext", "extraction")),
            literal_column("memory_fts").match(match),
            *_current_text(),
        )
        .order_by(rank, _TEXT.c.paper_id, _TEXT.c.kind, _TEXT.c.id, _TEXT.c.page)
    )


def _text_hits(rows: Sequence[Row[Any]]) -> tuple[CandidateTextHit, ...]:
    found = {}
    for row in rows:
        key = row.paper_id, row.kind
        if key not in found:
            found[key] = CandidateTextHit(
                row.paper_id,
                cast(CandidateTextKind, row.kind),
                row.id,
                int(row.page) if row.page is not None else None,
                row.source_version,
                float(row.rank),
            )
    return tuple(found[key] for key in sorted(found))


class SqlCandidateRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def snapshot(self, paper_id: str, terms_limit: int = 32) -> CandidateSnapshot:
        ids = select(paper.c.id).where(library_scope())
        with self._db.atomic():
            rows = _Rows(*(self._db.fetch(statement) for statement in _queries()))
            vectors = self._db.fetch(
                select(paper_embedding)
                .where(paper_embedding.c.paper_id.in_(ids))
                .order_by(
                    paper_embedding.c.paper_id,
                    paper_embedding.c.input_hash,
                    paper_embedding.c.embedder_version,
                )
            )
            source = next((row for row in rows.papers if row.id == paper_id), None)
            terms = candidate_terms(
                source.title if source else "", source.abstract if source else None, terms_limit
            )
            hits = self._db.fetch(_text_query(paper_id, terms))
        return CandidateSnapshot(
            _snapshot(rows), tuple(_embedding(row) for row in vectors), terms, _text_hits(hits)
        )
