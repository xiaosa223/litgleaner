"""把各仓储组合成 Store。"""

from contextlib import AbstractContextManager

from sqlalchemy import func, select

from litgleaner.infra.db.catalog_repo import SqlCatalogRepo
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.feedback_repo import SqlFeedbackRepo
from litgleaner.infra.db.inbox_repo import SqlInboxRepo
from litgleaner.infra.db.jobs_repo import SqlJobRepo, SqlRunRepo
from litgleaner.infra.db.judgments_repo import SqlJudgmentRepo
from litgleaner.infra.db.outputs_repo import SqlOutputRepo
from litgleaner.infra.db.papers_repo import SqlPaperRepo
from litgleaner.infra.db.qa_repo import SqlQARepo
from litgleaner.infra.db.reader_repo import SqlReaderRepo
from litgleaner.infra.db.schema import (
    annotation,
    artifact,
    assessment,
    delivery,
    digest,
    extraction,
    feedback,
    fulltext,
    inbox,
    library_entry,
    paper,
    reading_state,
    score,
    sighting,
    triage,
)
from litgleaner.infra.db.search_repo import SqlSearchRepo
from litgleaner.infra.db.translation_repo import SqlTranslationRepo

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
    reading_state,
    annotation,
    inbox,
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
        self._reader = SqlReaderRepo(self._db)
        self._translations = SqlTranslationRepo(self._db)
        self._inbox = SqlInboxRepo(self._db)
        self._catalog = SqlCatalogRepo(self._db)
        self._search = SqlSearchRepo(self._db)
        self._qa = SqlQARepo(self._db)

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

    @property
    def reader(self) -> SqlReaderRepo:
        return self._reader

    @property
    def translations(self) -> SqlTranslationRepo:
        return self._translations

    @property
    def inbox(self) -> SqlInboxRepo:
        return self._inbox

    @property
    def catalog(self) -> SqlCatalogRepo:
        return self._catalog

    @property
    def search(self) -> SqlSearchRepo:
        return self._search

    @property
    def qa(self) -> SqlQARepo:
        return self._qa

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
