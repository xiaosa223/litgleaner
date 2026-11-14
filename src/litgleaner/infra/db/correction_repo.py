"""用户修正的论文元数据：只追加，按字段取最新一条。"""

from collections.abc import Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.correction import Correction
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import paper_correction

_CHUNK = 500


class SqlCorrectionRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, item: Correction) -> None:
        self._db.write(
            insert(paper_correction).values(
                id=item.id,
                paper_id=item.paper_id,
                field=item.field,
                value=item.value,
                created_at=item.created_at,
            )
        )

    def latest(self, paper_ids: Iterable[str]) -> Mapping[str, Mapping[str, str]]:
        ids = sorted(set(paper_ids))
        result: dict[str, dict[str, str]] = {}
        for start in range(0, len(ids), _CHUNK):
            rows = self._db.fetch(
                select(paper_correction)
                .where(paper_correction.c.paper_id.in_(ids[start : start + _CHUNK]))
                .order_by(paper_correction.c.created_at, paper_correction.c.id)
            )
            for row in rows:  # 时间升序，后写的覆盖先写的
                result.setdefault(row.paper_id, {})[row.field] = row.value
        return result
