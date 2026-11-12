"""首次入库账本；重复入库沿用原事件，不追补任务。"""

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Row, select
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.paper_relation import MemoryAdmission
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import memory_admission


def _admission(row: Row[Any]) -> MemoryAdmission:
    return MemoryAdmission(
        row.id, row.paper_id, json.loads(row.configuration), row.created_at, row.baseline
    )


class SqlAdmissionRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, paper_id: str) -> MemoryAdmission | None:
        rows = self._db.fetch(
            select(memory_admission).where(memory_admission.c.paper_id == paper_id)
        )
        return _admission(rows[0]) if rows else None

    def add(self, paper_id: str, configuration: Mapping[str, Any], now: str) -> MemoryAdmission:
        with self._db.atomic():
            if (found := self.get(paper_id)) is not None:
                return found
            self._db.write(
                insert(memory_admission).values(
                    paper_id=paper_id,
                    configuration=json.dumps(
                        dict(configuration), ensure_ascii=False, allow_nan=False
                    ),
                    created_at=now,
                    baseline=False,
                )
            )
            found = self.get(paper_id)
            assert found is not None
            return found

    def before(self, admission_id: int) -> tuple[str, ...]:
        if type(admission_id) is not int or admission_id <= 0:
            raise ValueError("admission_id_invalid")
        return tuple(
            row.paper_id
            for row in self._db.fetch(
                select(memory_admission.c.paper_id)
                .where(memory_admission.c.id < admission_id)
                .order_by(memory_admission.c.id)
            )
        )
