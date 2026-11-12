"""FTS5 检索：三元分词支持中文子串；短于三字的词用同一索引表的字面匹配。"""

import re
from typing import Any, cast

from sqlalchemy import Row, text
from sqlalchemy.sql.elements import TextClause

from litgleaner.domain.document import Block
from litgleaner.domain.search_text import normalize_search_text, search_excerpt
from litgleaner.infra.db import embedding_repo
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.qa_search_repo import qa_hits
from litgleaner.infra.db.search_index import RANK_SQL
from litgleaner.ports.search import (
    Authorship,
    QASearchHit,
    SearchCandidate,
    SearchDocument,
    SearchHit,
    SearchKind,
    SearchSnapshot,
)

_WORDS = re.compile(r"\w+", re.UNICODE)
_KINDS = "('paper', 'fulltext', 'note', 'highlight', 'extraction', 'qa', 'concept')"


def _terms(query: str) -> list[str]:
    return list(dict.fromkeys(_WORDS.findall(normalize_search_text(query))))[:64]


def _conditions(terms: list[str]) -> tuple[str, dict[str, str], bool]:
    long = [term for term in terms if len(term) >= 3]
    conditions, params = [], {}
    if long:
        conditions.append("memory_fts MATCH :match")
        # 用户的 OR、引号、括号都按词检索，不能成为 FTS 操作符。
        params["match"] = (
            "{search_title search_text} : (" + " AND ".join(f'"{term}"' for term in long) + ")"
        )
    for index, term in enumerate(term for term in terms if len(term) < 3):
        name = f"short_{index}"
        conditions.append(
            f"(f.search_title LIKE :{name} ESCAPE '\\' OR f.search_text LIKE :{name} ESCAPE '\\')"
        )
        params[name] = "%" + term.replace("_", "\\_") + "%"
    return " AND ".join(conditions), params, bool(long)


def _lexical_statement(terms: list[str], limit: int) -> TextClause:
    conditions, params, ranked = _conditions(terms)
    rank = RANK_SQL if ranked else "0"
    return text(
        "SELECT f.kind, f.id, f.paper_id, p.title, f.text, f.page, f.authorship, "
        f"{rank} AS rank "
        "FROM memory_fts AS f JOIN paper AS p ON p.id = f.paper_id "
        f"WHERE f.kind IN {_KINDS} AND ({conditions or '0'}) "
        "AND (f.kind != 'fulltext' OR EXISTS "
        "(SELECT 1 FROM fulltext WHERE paper_id = f.paper_id AND doc_key = f.source_version)) "
        "ORDER BY rank, f.kind, f.paper_id, f.id LIMIT :limit"
    ).bindparams(**params, limit=limit)


def _candidate(row: Row[Any]) -> SearchCandidate:
    return SearchCandidate(
        cast(SearchKind, row.kind),
        row.id,
        row.paper_id,
        row.title,
        row.text,
        int(row.page) if row.page is not None else None,
        cast(Authorship, row.authorship),
        float(row.rank),
    )


class SqlSearchRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def qa_hits(
        self, query: str, paper_ids: tuple[str, ...], limit: int = 64
    ) -> tuple[QASearchHit, ...]:
        return qa_hits(self._db, query, paper_ids, limit)

    def find(self, query: str, limit: int) -> tuple[SearchHit, ...]:
        terms = _terms(query)
        if not terms:
            return ()
        statement = _lexical_statement(terms, limit)
        return tuple(
            SearchHit(
                cast(SearchKind, row.kind),
                row.id,
                row.paper_id,
                row.title,
                search_excerpt(row.text, terms),
                int(row.page) if row.page is not None else None,
                cast(Authorship, row.authorship),
            )
            for row in self._db.fetch(statement)
        )

    def snapshot(self, query: str, limit: int = 512) -> SearchSnapshot:
        terms = _terms(query)
        statement = _lexical_statement(terms, max(0, min(limit, 512)))
        with self._db.atomic():
            lexical_rows = self._db.fetch(statement)
            embedding_rows = embedding_repo.snapshot_rows(self._db)
        candidates = tuple(_candidate(row) for row in lexical_rows)
        try:
            embeddings = embedding_repo._snapshot(embedding_rows)
        except Exception:  # 仅转换降级；数据库查询失败在此之前直接传播。
            return SearchSnapshot(candidates, tuple(terms), None, embedding_problem=True)
        return SearchSnapshot(candidates, tuple(terms), embeddings)

    def pending_documents(self) -> tuple[SearchDocument, ...]:
        rows = self._db.fetch(
            text(
                "SELECT paper_id, doc_key FROM fulltext WHERE doc_key IS NOT NULL AND NOT EXISTS "
                "(SELECT 1 FROM memory_fts WHERE kind = 'document' "
                "AND memory_fts.paper_id = fulltext.paper_id AND source_version = doc_key) "
                "ORDER BY paper_id"
            )
        )
        return tuple(SearchDocument(row.paper_id, row.doc_key) for row in rows)

    def replace_document(self, paper_id: str, doc_key: str, blocks: tuple[Block, ...]) -> None:
        with self._db.atomic():
            self._db.write(
                text(
                    "DELETE FROM memory_fts WHERE paper_id = :paper "
                    "AND kind IN ('fulltext', 'document')"
                ).bindparams(paper=paper_id)
            )
            for index, block in enumerate(blocks):
                if block.text.strip():
                    self._insert(
                        paper_id, "fulltext", str(index), block.page, doc_key, content=block.text
                    )
            self._insert(paper_id, "document", paper_id, None, doc_key, content="")

    def _insert(
        self, paper_id: str, kind: str, id_: str, page: int | None, version: str, *, content: str
    ) -> None:
        self._db.write(
            text(
                "INSERT INTO memory_fts"
                "(kind,id,paper_id,page,authorship,source_version,title,text,"
                "search_title,search_text) "
                "VALUES (:kind,:id,:paper,:page,'author',:version,'',:content,'',:normalized)"
            ).bindparams(
                kind=kind,
                id=id_,
                paper=paper_id,
                page=page,
                version=version,
                content=content,
                normalized=normalize_search_text(content),
            )
        )
