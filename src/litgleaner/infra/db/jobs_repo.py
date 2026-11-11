"""任务队列和运行记录。"""

import json
from typing import Any

from sqlalchemy import Row, func, select, update
from sqlalchemy.dialects.sqlite import insert

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import job, llm_call
from litgleaner.ports.store import Job, JobSpec, JobStatus, LLMCall


def _to_job(row: Row[Any]) -> Job:
    return Job(
        key=row.key,
        stage=row.stage,
        payload=json.loads(row.payload),
        status=row.status,
        attempts=row.attempts,
        last_error=row.last_error,
        first_deferred_at=row.first_deferred_at,
    )


class SqlJobRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def enqueue(self, spec: JobSpec, now: str, *, reopen: bool = False) -> bool:
        """幂等：同样的键只会执行一次。reopen 时，已完成的任务会被重新放回队列。"""
        statement = insert(job).values(
            key=spec.key,
            stage=spec.stage,
            payload=json.dumps(spec.payload, ensure_ascii=False, sort_keys=True),
            status="pending",
            attempts=0,
            next_run_at=now,
            created_at=now,
            updated_at=now,
        )
        if reopen:
            statement = statement.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "status": "pending",
                    "attempts": 0,
                    "next_run_at": now,
                    "updated_at": now,
                    "first_deferred_at": None,
                },
                where=job.c.status == "done",
            )
        else:
            statement = statement.on_conflict_do_nothing()
        return self._db.write(statement) > 0

    def reopen(self, key: str, now: str) -> None:
        self._db.write(
            update(job)
            .where(job.c.key == key)
            .values(
                status="pending",
                attempts=0,
                last_error=None,
                next_run_at=now,
                updated_at=now,
                first_deferred_at=None,
            )
        )

    def get(self, key: str) -> Job | None:
        rows = self._db.fetch(select(job).where(job.c.key == key))
        return _to_job(rows[0]) if rows else None

    def due(self, stage: str, now: str) -> tuple[Job, ...]:
        rows = self._db.fetch(
            select(job)
            .where(job.c.stage == stage, job.c.status == "pending", job.c.next_run_at <= now)
            .order_by(job.c.created_at, job.c.key)
        )
        return tuple(_to_job(row) for row in rows)

    def mark_done(self, key: str, now: str) -> None:
        self._db.write(
            update(job)
            .where(job.c.key == key)
            .values(status="done", last_error=None, updated_at=now)
        )

    def mark_retry(self, key: str, error: str, next_run_at: str, now: str) -> None:
        self._db.write(
            update(job)
            .where(job.c.key == key)
            .values(
                attempts=job.c.attempts + 1,
                last_error=error,
                next_run_at=next_run_at,
                updated_at=now,
            )
        )

    def postpone(self, key: str, error: str, next_run_at: str, now: str) -> None:
        """推迟但不计失败次数，用于额度用完、外部任务仍在处理等情况。"""
        self._db.write(
            update(job)
            .where(job.c.key == key)
            .values(
                last_error=error,
                next_run_at=next_run_at,
                updated_at=now,
                first_deferred_at=func.coalesce(job.c.first_deferred_at, now),
            )
        )

    def mark_dead(self, key: str, error: str, now: str) -> None:
        self._db.write(
            update(job)
            .where(job.c.key == key)
            .values(status="dead", attempts=job.c.attempts + 1, last_error=error, updated_at=now)
        )

    def list_by_status(self, status: JobStatus) -> tuple[Job, ...]:
        rows = self._db.fetch(select(job).where(job.c.status == status).order_by(job.c.key))
        return tuple(_to_job(row) for row in rows)

    def retry_dead(self, now: str) -> int:
        return self._db.write(
            update(job)
            .where(job.c.status == "dead")
            .values(
                status="pending",
                attempts=0,
                next_run_at=now,
                updated_at=now,
                first_deferred_at=None,
            )
        )


class SqlRunRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def record_llm_call(self, call: LLMCall, now: str) -> None:
        self._db.write(
            llm_call.insert().values(
                task=call.task,
                model=call.model,
                prompt_version=call.prompt_version,
                input_tokens=call.input_tokens,
                output_tokens=call.output_tokens,
                cost_usd=call.cost_usd,
                ok=call.ok,
                paper_id=call.paper_id,
                created_at=now,
            )
        )

    def llm_cost_since(self, since: str) -> float:
        rows = self._db.fetch(
            select(func.coalesce(func.sum(llm_call.c.cost_usd), 0.0).label("total")).where(
                llm_call.c.created_at >= since
            )
        )
        return float(rows[0].total)

    def llm_cost_for_paper(self, paper_id: str) -> float:
        rows = self._db.fetch(
            select(func.coalesce(func.sum(llm_call.c.cost_usd), 0.0).label("total")).where(
                llm_call.c.paper_id == paper_id
            )
        )
        return float(rows[0].total)

    def llm_call_count(self, task: str | None = None) -> int:
        statement = select(func.count().label("n")).select_from(llm_call)
        if task:
            statement = statement.where(llm_call.c.task == task)
        return int(self._db.fetch(statement)[0].n)

    def average_llm_cost(self, task: str) -> float | None:
        rows = self._db.fetch(
            select(func.avg(llm_call.c.cost_usd).label("average")).where(llm_call.c.task == task)
        )
        return float(rows[0].average) if rows[0].average is not None else None
