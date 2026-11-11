"""FTS5 检索：三元分词支持中文子串；短于三字的词用同一索引表的字面匹配。"""

import re
from typing import cast

from sqlalchemy import text

from litgleaner.domain.document import Block
from litgleaner.infra.db.database import Database
from litgleaner.ports.search import Authorship, SearchDocument, SearchHit, SearchKind

_WORDS = re.compile(r"\w+", re.UNICODE)
_KINDS = "('paper', 'fulltext', 'note', 'highlight', 'extraction', 'qa')"
_SNIPPET_CHARS = 320


def _excerpt(content: str, terms: list[str]) -> str:
    lowered = content.lower()
    positions = [at for term in terms if (at := lowered.find(term)) >= 0]
    start = max(0, min(positions, default=0) - 60)
    end = start + _SNIPPET_CHARS
    return ("…" if start else "") + content[start:end] + ("…" if end < len(content) else "")


def _conditions(terms: list[str]) -> tuple[str, dict[str, str], bool]:
    long = [term for term in terms if len(term) >= 3]
    conditions, params = [], {}
    if long:
        conditions.append("memory_fts MATCH :match")
        # 用户的 OR、引号、括号都按词检索，不能成为 FTS 操作符。
        params["match"] = " AND ".join(f'"{term}"' for term in long)
    for index, term in enumerate(term for term in terms if len(term) < 3):
        name = f"short_{index}"
        conditions.append(f"(f.title LIKE :{name} ESCAPE '\\' OR f.text LIKE :{name} ESCAPE '\\')")
        params[name] = "%" + term.replace("_", "\\_") + "%"
    return " AND ".join(conditions), params, bool(long)


class SqlSearchRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def find(self, query: str, limit: int) -> tuple[SearchHit, ...]:
        terms = list(dict.fromkeys(_WORDS.findall(query.lower())))[:64]
        if not terms:
            return ()
        conditions, params, ranked = _conditions(terms)
        rank = "bm25(memory_fts, 0, 0, 0, 0, 0, 0, 4, 1)" if ranked else "0"
        statement = text(
            "SELECT f.kind, f.id, f.paper_id, p.title, f.text, f.page, f.authorship, "
            f"{rank} AS rank "
            "FROM memory_fts AS f JOIN paper AS p ON p.id = f.paper_id "
            f"WHERE f.kind IN {_KINDS} AND ({conditions}) "
            "AND (f.kind != 'fulltext' OR EXISTS "
            "(SELECT 1 FROM fulltext WHERE paper_id = f.paper_id AND doc_key = f.source_version)) "
            "ORDER BY rank, f.kind, f.paper_id, f.id LIMIT :limit"
        ).bindparams(**params, limit=limit)
        return tuple(
            SearchHit(
                cast(SearchKind, row.kind),
                row.id,
                row.paper_id,
                row.title,
                _excerpt(row.text, terms),
                int(row.page) if row.page is not None else None,
                cast(Authorship, row.authorship),
            )
            for row in self._db.fetch(statement)
        )

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
                "(kind,id,paper_id,page,authorship,source_version,title,text) "
                "VALUES (:kind,:id,:paper,:page,'author',:version,'',:content)"
            ).bindparams(
                kind=kind, id=id_, paper=paper_id, page=page, version=version, content=content
            )
        )
