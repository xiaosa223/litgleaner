"""历史重提取固定四次短快照；仅规范来源的复核固定三次读取。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, Select, and_, case, false, func, select

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.papers_repo import _to_paper
from litgleaner.infra.db.schema import extraction, fulltext, job, paper
from litgleaner.infra.db.stale_repo import _fulltexts
from litgleaner.ports.reextract import (
    ReextractFingerprint,
    ReextractSnapshot,
    metadata_revision,
)
from litgleaner.ports.store import Job


@dataclass(frozen=True)
class _Rows:
    papers: Sequence[Row[Any]]
    fulltexts: Sequence[Row[Any]]
    extractions: Sequence[Row[Any]]


def _scope(paper_ids: tuple[str, ...] | None) -> Select[Any]:
    if paper_ids is None:
        ids = select(extraction.c.paper_id)
    else:
        requested = func.json_each(json.dumps(paper_ids)).table_valued("value")
        ids = select(requested.c.value)
    return select(paper.c.id).where(paper.c.id.in_(ids))


def _queries(paper_ids: tuple[str, ...] | None) -> tuple[Select[Any], ...]:
    ids = _scope(paper_ids)
    return (
        select(paper).where(paper.c.id.in_(ids)).order_by(paper.c.id),
        select(fulltext).where(fulltext.c.paper_id.in_(ids)).order_by(fulltext.c.paper_id),
        select(
            extraction.c.id,
            extraction.c.paper_id,
            extraction.c.input_hash,
            extraction.c.extractor_version,
        )
        .where(extraction.c.paper_id.in_(ids))
        .order_by(extraction.c.id),
    )


def _job_query(paper_ids: tuple[str, ...] | None) -> Select[Any]:
    valid_scope = case(
        (
            func.json_valid(job.c.payload),
            and_(
                func.json_type(job.c.payload, "$.paper_id") == "text",
                func.json_extract(job.c.payload, "$.paper_id").in_(_scope(paper_ids)),
            ),
        ),
        else_=false(),
    )
    return select(job).where(job.c.stage == "reextract", valid_scope).order_by(job.c.key)


def _jobs(rows: Sequence[Row[Any]], ids: frozenset[str]) -> tuple[Job, ...]:
    result = []
    for row in rows:
        try:
            payload = json.loads(row.payload)
        except (ValueError, TypeError, RecursionError):
            continue
        if not isinstance(payload, dict):
            continue
        paper_id = payload.get("paper_id")
        if not isinstance(paper_id, str) or not paper_id.strip() or paper_id not in ids:
            continue
        result.append(
            Job(
                row.key,
                row.stage,
                payload,
                row.status,
                row.attempts,
                row.last_error,
                row.first_deferred_at,
            )
        )
    return tuple(result)


def _snapshot(rows: _Rows, jobs: Sequence[Row[Any]] = ()) -> ReextractSnapshot:
    papers = tuple(_to_paper(row) for row in rows.papers)
    fulltexts = _fulltexts(rows.fulltexts)
    extractions = tuple(
        ReextractFingerprint(row.id, row.paper_id, row.input_hash, row.extractor_version)
        for row in rows.extractions
    )
    return ReextractSnapshot(
        papers,
        fulltexts,
        extractions,
        _jobs(jobs, frozenset(item.id for item in papers)),
        metadata_revision(papers, fulltexts, extractions),
    )


class SqlReextractRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def snapshot(self, paper_ids: tuple[str, ...] | None = None) -> ReextractSnapshot:
        with self._db.atomic():
            rows = _Rows(*(self._db.fetch(statement) for statement in _queries(paper_ids)))
            jobs = self._db.fetch(_job_query(paper_ids))
        return _snapshot(rows, jobs)

    def revision(self, paper_ids: tuple[str, ...] | None = None) -> str:
        with self._db.atomic():
            rows = _Rows(*(self._db.fetch(statement) for statement in _queries(paper_ids)))
        return _snapshot(rows).revision
