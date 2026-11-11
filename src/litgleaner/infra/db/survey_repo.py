"""调研预览只消费一次；恢复工作和最终轮次保存首写事实。"""

import json

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.survey import NewSurveyRound, SurveyPreview, SurveyRound, SurveyWork
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import survey_preview, survey_round, survey_work

_HELD_PAPERS = """
SELECT DISTINCT item.value AS paper_id
FROM survey_work w
JOIN job parent ON parent.key='survey:'||w.run_id
JOIN json_each(w.data,'$.paper_ids') item
LEFT JOIN survey_round r ON r.run_id=w.run_id AND r.iteration=w.iteration
WHERE parent.status IN ('pending','dead') OR (
    parent.status='done' AND (
        coalesce(json_extract(r.data,'$.cancelled'),0)=1 OR
        coalesce(json_array_length(r.data,'$.failed_jobs'),0)>0
    ) AND NOT EXISTS (
        SELECT 1 FROM survey_round later
        JOIN json_each(later.data,'$.paper_ids') released
        WHERE later.topic_id=w.topic_id AND later.number>r.number
          AND coalesce(json_extract(later.data,'$.cancelled'),0)=0
          AND coalesce(json_array_length(later.data,'$.failed_jobs'),0)=0
          AND released.value=item.value
    )
)
"""


def _nonempty(*values: str) -> None:
    if any(not value.strip() for value in values):
        raise ValueError("survey_identifier_empty")


class SqlSurveyRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add_preview(self, preview: SurveyPreview) -> bool:
        _nonempty(preview.id, preview.topic_id, preview.topic_hash, preview.fingerprint)
        return bool(
            self._db.write(
                insert(survey_preview)
                .values(
                    id=preview.id,
                    topic_id=preview.topic_id,
                    topic_hash=preview.topic_hash,
                    fingerprint=preview.fingerprint,
                    created_at=preview.created_at,
                    data=preview.model_dump_json(),
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
        )

    def preview(self, plan_id: str) -> SurveyPreview | None:
        rows = self._db.fetch(select(survey_preview.c.data).where(survey_preview.c.id == plan_id))
        return SurveyPreview.model_validate_json(rows[0].data) if rows else None

    def latest_preview(self, topic_id: str) -> SurveyPreview | None:
        rows = self._db.fetch(
            select(survey_preview.c.data)
            .where(
                survey_preview.c.topic_id == topic_id,
                func.coalesce(func.json_extract(survey_preview.c.data, "$.internal"), 0) == 0,
            )
            .order_by(survey_preview.c.created_at.desc(), survey_preview.c.id.desc())
            .limit(1)
        )
        return SurveyPreview.model_validate_json(rows[0].data) if rows else None

    def held_paper_ids(self) -> frozenset[str]:
        return frozenset(row.paper_id for row in self._db.fetch(text(_HELD_PAPERS)))

    def accept(self, plan_id: str, run_id: str, now: str) -> bool:
        _nonempty(run_id, now)
        return bool(
            self._db.write(
                update(survey_preview)
                .where(survey_preview.c.id == plan_id, survey_preview.c.accepted_run.is_(None))
                .values(accepted_run=run_id, accepted_at=now)
            )
        )

    def accepted_run(self, plan_id: str) -> str | None:
        rows = self._db.fetch(
            select(survey_preview.c.accepted_run).where(survey_preview.c.id == plan_id)
        )
        return rows[0].accepted_run if rows else None

    def save_work(self, work: SurveyWork) -> bool:
        with self._db.atomic():
            if self.get_work(work.run_id, work.iteration) is not None:
                return False
            self._check_plan(work.plan_id, work.run_id, work.topic_id, work.topic_hash)
            self._check_papers(work.paper_ids)
            return bool(
                self._db.write(
                    insert(survey_work)
                    .values(
                        run_id=work.run_id,
                        iteration=work.iteration,
                        plan_id=work.plan_id,
                        scope_id=work.scope_id,
                        topic_id=work.topic_id,
                        topic_hash=work.topic_hash,
                        started_at=work.started_at,
                        data=work.model_dump_json(),
                    )
                    .on_conflict_do_nothing(index_elements=["run_id", "iteration"])
                )
            )

    def get_work(self, run_id: str, iteration: int) -> SurveyWork | None:
        rows = self._db.fetch(
            select(survey_work.c.data).where(
                survey_work.c.run_id == run_id,
                survey_work.c.iteration == iteration,
            )
        )
        return SurveyWork.model_validate_json(rows[0].data) if rows else None

    def append_round(self, item: NewSurveyRound) -> SurveyRound:
        with self._db.atomic():
            if (saved := self.round_for(item.run_id, item.iteration)) is not None:
                return saved
            self._check_plan(item.plan_id, item.run_id, item.topic_id, item.topic_hash)
            work = self.get_work(item.run_id, item.iteration)
            if work is None or (work.plan_id, work.topic_id, work.topic_hash) != (
                item.plan_id,
                item.topic_id,
                item.topic_hash,
            ):
                raise ValueError("survey_work_missing_or_mismatched")
            self._check_papers(item.paper_ids)
            rows = self._db.fetch(
                select(func.coalesce(func.max(survey_round.c.number), 0)).where(
                    survey_round.c.topic_id == item.topic_id
                )
            )
            result = SurveyRound(**item.model_dump(), number=int(rows[0][0]) + 1)
            columns = (
                "topic_id",
                "number",
                "topic_hash",
                "run_id",
                "iteration",
                "plan_id",
                "started_at",
                "finished_at",
                "candidates",
                "new_papers",
                "passed",
                "new_passed",
                "undetermined",
                "cost_usd",
            )
            self._db.write(
                insert(survey_round).values(
                    **result.model_dump(include=set(columns)), data=result.model_dump_json()
                )
            )
            return result

    def rounds(self, topic_id: str, limit: int = 100) -> tuple[SurveyRound, ...]:
        if type(limit) is not int or limit < 1:
            raise ValueError("survey_limit_invalid")
        rows = self._db.fetch(
            select(survey_round.c.data)
            .where(survey_round.c.topic_id == topic_id)
            .order_by(survey_round.c.number.desc())
            .limit(limit)
        )
        return tuple(SurveyRound.model_validate_json(row.data) for row in reversed(rows))

    def round_for(self, run_id: str, iteration: int) -> SurveyRound | None:
        rows = self._db.fetch(
            select(survey_round.c.data).where(
                survey_round.c.run_id == run_id, survey_round.c.iteration == iteration
            )
        )
        return SurveyRound.model_validate_json(rows[0].data) if rows else None

    def _check_plan(self, plan_id: str, run_id: str, topic_id: str, topic_hash: str) -> None:
        plan = self.preview(plan_id)
        if (
            plan is None
            or self.accepted_run(plan_id) != run_id
            or (plan.topic_id, plan.topic_hash) != (topic_id, topic_hash)
        ):
            raise ValueError("survey_plan_missing_or_mismatched")

    def _check_papers(self, paper_ids: tuple[str, ...]) -> None:
        rows = self._db.fetch(
            text(
                "SELECT item.value FROM json_each(:ids) item LEFT JOIN paper p ON p.id=item.value "
                "WHERE p.id IS NULL LIMIT 1"
            ).bindparams(ids=json.dumps(paper_ids))
        )
        if rows:
            raise ValueError("survey_paper_missing")
