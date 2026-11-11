"""参考文献只追加；反向链接只采用事实 ID 或唯一的精确别名匹配。"""

import json
from dataclasses import asdict
from typing import Any, cast

from sqlalchemy import Row, select, text
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.citation import Citation
from litgleaner.domain.identity import Alias
from litgleaner.domain.reading import BBox
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import citation as citation_table


def _citation(row: Row[Any]) -> Citation:
    return Citation(
        row.citing_paper_id,
        row.position,
        row.raw_text,
        row.source,
        row.source_position,
        row.cited_paper_id,
        row.cited_title,
        tuple(Alias(alias["scheme"], alias["value"]) for alias in json.loads(row.cited_aliases)),
        tuple(json.loads(row.contexts)),
        tuple(json.loads(row.intents)),
        None if row.influential is None else bool(row.influential),
        row.created_at,
        row.page,
        cast(BBox, tuple(json.loads(row.bbox))) if row.bbox is not None else None,
    )


class SqlCitationRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, citation: Citation) -> bool:
        item = citation
        return bool(
            self._db.write(
                insert(citation_table)
                .values(
                    citing_paper_id=item.citing_paper_id,
                    position=item.position,
                    raw_text=item.raw_text,
                    source=item.source,
                    source_position=item.source_position,
                    cited_paper_id=item.cited_paper_id,
                    cited_title=item.cited_title,
                    cited_aliases=json.dumps(
                        [asdict(alias) for alias in item.cited_aliases], ensure_ascii=False
                    ),
                    contexts=json.dumps(item.contexts, ensure_ascii=False),
                    intents=json.dumps(item.intents, ensure_ascii=False),
                    influential=item.influential,
                    created_at=item.created_at,
                    page=item.page,
                    bbox=json.dumps(item.bbox) if item.bbox is not None else None,
                )
                .on_conflict_do_nothing(index_elements=["citing_paper_id", "position"])
            )
        )

    def outgoing(self, paper_id: str) -> tuple[Citation, ...]:
        statement = (
            select(citation_table)
            .where(citation_table.c.citing_paper_id == paper_id)
            .order_by(
                citation_table.c.source,
                citation_table.c.source_position,
                citation_table.c.position,
            )
        )
        return tuple(_citation(row) for row in self._db.fetch(statement))

    def incoming(self, paper_id: str) -> tuple[Citation, ...]:
        statement = text(
            "WITH matched AS (SELECT c.citing_paper_id,c.position FROM citation c, "
            "json_each(c.cited_aliases) AS item JOIN paper_alias a ON "
            "a.scheme=json_extract(item.value,'$.scheme') AND "
            "a.value=json_extract(item.value,'$.value') WHERE c.cited_paper_id IS NULL "
            "GROUP BY c.citing_paper_id,c.position "
            "HAVING count(DISTINCT a.paper_id)=1 AND max(a.paper_id)=:paper) "
            "SELECT c.* FROM citation c WHERE c.cited_paper_id=:paper OR EXISTS "
            "(SELECT 1 FROM matched m WHERE m.citing_paper_id=c.citing_paper_id "
            "AND m.position=c.position) "
            "ORDER BY c.citing_paper_id,c.source,c.source_position,c.position"
        ).bindparams(paper=paper_id)
        return tuple(_citation(row) for row in self._db.fetch(statement))
