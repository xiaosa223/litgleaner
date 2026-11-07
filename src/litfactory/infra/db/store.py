"""把各仓储组合成 Store。"""

from contextlib import AbstractContextManager

from sqlalchemy import func, select

from litfactory.infra.db.database import Database
from litfactory.infra.db.feedback_repo import SqlFeedbackRepo
from litfactory.infra.db.jobs_repo import SqlJobRepo, SqlRunRepo
from litfactory.infra.db.judgments_repo import SqlJudgmentRepo
from litfactory.infra.db.outputs_repo import SqlOutputRepo
from litfactory.infra.db.papers_repo import SqlPaperRepo
from litfactory.infra.db.schema import (
    artifact,
    assessment,
    delivery,
    digest,
    extraction,
    feedback,
    fulltext,
    library_entry,
    paper,
    score,
    sighting,
    triage,
)

_COUNTED = (
    paper,
    sighting,
    triage,
    fulltext,
    extraction,
    assessment,
    score,
    digest,
    delivery,
    feedback,
    library_entry,
)


class SqlStore:
    def __init__(self, url: str) -> None:
        self._db = Database(url)
        self._papers = SqlPaperRepo(self._db)
        self._judgments = SqlJudgmentRepo(self._db)
        self._outputs = SqlOutputRepo(self._db)
        self._feedback = SqlFeedbackRepo(self._db)
        self._jobs = SqlJobRepo(self._db)
        self._runs = SqlRunRepo(self._db)

    @property
    def papers(self) -> SqlPaperRepo:
        return self._papers

    @property
    def judgments(self) -> SqlJudgmentRepo:
        return self._judgments

    @property
    def outputs(self) -> SqlOutputRepo:
        return self._outputs

    @property
    def feedback(self) -> SqlFeedbackRepo:
        return self._feedback

    @property
    def jobs(self) -> SqlJobRepo:
        return self._jobs

    @property
    def runs(self) -> SqlRunRepo:
        return self._runs

    def atomic(self) -> AbstractContextManager[None]:
        return self._db.atomic()

    def stats(self) -> dict[str, int]:
        counts = {
            table.name: int(self._db.fetch(select(func.count()).select_from(table))[0][0])
            for table in _COUNTED
        }
        for kind in ("pdf", "doc", "parser_raw"):
            statement = select(func.count()).select_from(artifact).where(artifact.c.kind == kind)
            counts[f"artifact_{kind}"] = int(self._db.fetch(statement)[0][0])
        return counts

    def dispose(self) -> None:
        self._db.dispose()
