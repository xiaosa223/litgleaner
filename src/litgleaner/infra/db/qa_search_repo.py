"""问答候选只召回仍对应规范记录的完整作者片段与用户文字。"""

import json
import re
from typing import Any, cast

from sqlalchemy import TextClause, text
from sqlalchemy.exc import OperationalError

from litgleaner.domain.search_text import normalize_search_text
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.search_index import RANK_SQL
from litgleaner.ports.search import Authorship, QASearchHit, SearchKind

_WORDS = re.compile(r"\w+", re.UNICODE)
_CANONICAL = (
    "((f.kind='fulltext' AND f.authorship='author' AND EXISTS "
    "(SELECT 1 FROM fulltext d WHERE d.paper_id=f.paper_id AND d.doc_key=f.source_version)) "
    "OR (f.kind IN ('note','highlight') AND f.authorship='user' AND EXISTS "
    "(SELECT 1 FROM annotation a WHERE "
    "a.id=substr(f.id,1,length(f.id)-length(:body_suffix)) "
    "AND f.id=a.id || :body_suffix AND a.paper_id=f.paper_id "
    "AND a.kind=f.kind AND a.deleted_at IS NULL AND a.body=f.text AND a.page IS f.page)) "
    "OR (f.kind='qa' AND f.authorship='user' AND EXISTS "
    "(SELECT 1 FROM qa_message q WHERE q.id=f.id AND q.paper_id=f.paper_id "
    "AND q.role='question' AND q.text=f.text AND q.page IS f.page)))"
)
_COLUMNS = "f.kind,f.id,f.paper_id,f.text,f.page,f.authorship,f.source_version"
_WHERE = "f.paper_id IN (SELECT value FROM json_each(:paper_ids)) AND " + _CANONICAL


def _statement(query: str, paper_ids: tuple[str, ...], limit: int) -> TextClause | None:
    terms = tuple(dict.fromkeys(_WORDS.findall(normalize_search_text(query[:8192]))))[:64]
    if not terms or not paper_ids or type(limit) is not int or limit <= 0:
        return None
    long, short = [term for term in terms if len(term) >= 3], [t for t in terms if len(t) < 3]
    params: dict[str, Any] = {
        "paper_ids": json.dumps(paper_ids),
        "limit": min(limit, 64),
        "body_suffix": ":body",
    }
    selects: list[str] = []
    if long:
        params["match"] = (
            "{search_title search_text} : (" + " OR ".join(f'"{term}"' for term in long) + ")"
        )
        selects.append(
            f"SELECT {_COLUMNS},{RANK_SQL} AS rank "
            f"FROM memory_fts AS f WHERE {_WHERE} AND memory_fts MATCH :match"
        )
    if short:
        clauses = []
        for index, term in enumerate(short):
            name = f"short_{index}"
            params[name] = "%" + term.replace("_", "\\_") + "%"
            clauses.append(
                f"(f.search_title LIKE :{name} ESCAPE '\\' "
                f"OR f.search_text LIKE :{name} ESCAPE '\\')"
            )
        exclude = (
            " AND f.rowid NOT IN (SELECT rowid FROM memory_fts WHERE memory_fts MATCH :match)"
            if long
            else ""
        )
        selects.append(
            f"SELECT {_COLUMNS},0 AS rank FROM memory_fts AS f "
            f"WHERE {_WHERE} AND ({' OR '.join(clauses)}){exclude}"
        )
    return text(
        " UNION ALL ".join(selects) + " ORDER BY rank,kind,paper_id,id LIMIT :limit"
    ).bindparams(**params)


def qa_hits(
    db: Database, query: str, paper_ids: tuple[str, ...], limit: int = 64
) -> tuple[QASearchHit, ...]:
    statement = _statement(query, paper_ids, limit)
    if statement is None:
        return ()
    try:
        rows = db.fetch(statement)
    except OperationalError as error:
        if "no such table: memory_fts" in str(error.orig):
            return ()
        raise
    return tuple(
        QASearchHit(
            cast(SearchKind, row.kind),
            row.id,
            row.paper_id,
            row.text,
            int(row.page) if row.page is not None else None,
            cast(Authorship, row.authorship),
            row.source_version,
            float(row.rank),
        )
        for row in rows
    )
