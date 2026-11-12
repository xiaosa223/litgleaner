"""发现阶段：调用信号源，原始响应先落盘，再去重入库。"""

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from ulid import ULID

from litgleaner.application.citations import ingest_source_citations
from litgleaner.application.codec import compress
from litgleaner.application.context import AppContext
from litgleaner.application.source_fetch import fetch, scopes
from litgleaner.application.stages.base import Stage
from litgleaner.application.topics import active_topics
from litgleaner.domain.identity import (
    ExistingPaper,
    decide_resolution,
    record_aliases,
    title_key,
)
from litgleaner.domain.paper import Paper, SourceRecord
from litgleaner.domain.query import CitationWatch, plan_queries, query_from_payload
from litgleaner.ports.store import JobSpec, PaperRepo

logger = logging.getLogger(__name__)


def _new_paper(record: SourceRecord, key: str, now: str) -> Paper:
    return Paper(
        id=str(ULID()),
        title=record.title,
        title_key=key,
        abstract=record.abstract,
        authors=record.authors,
        venue=record.venue,
        issn=record.issn,
        published_at=record.published_at.isoformat() if record.published_at else None,
        first_seen_at=now,
        publication_types=record.publication_types,
        citation_count=record.citation_count,
    )


def _title_matches(papers: PaperRepo, key: str) -> tuple[ExistingPaper, ...]:
    if not key:
        return ()
    return tuple(ExistingPaper(p.id, p.authors) for p in papers.find_by_title_key(key))


def resolve_record(papers: PaperRepo, record: SourceRecord, now: str) -> str:
    """返回这条记录对应的论文 ID，必要时新建。冲突只记录，不自动合并。"""
    aliases = record_aliases(record)
    key = title_key(record.title)
    hits = papers.find_ids_by_aliases(aliases)
    matches = _title_matches(papers, key) if not hits else ()
    decision = decide_resolution(record, hits, matches)
    detail = {"title": record.title, "candidates": list(decision.conflicting_ids)}
    if decision.kind == "conflict":
        paper_id = decision.conflicting_ids[0]
        papers.log_merge(paper_id, "alias_conflict", "pending", detail, now)
        return paper_id
    if decision.kind == "existing" and decision.paper_id:
        papers.add_aliases(decision.paper_id, aliases)
        papers.fill_missing(decision.paper_id, _optional_fields(record))
        papers.log_merge(decision.paper_id, str(decision.reason), "applied", detail, now)
        return decision.paper_id
    paper = _new_paper(record, key, now)
    papers.insert(paper)
    papers.add_aliases(paper.id, aliases)
    if decision.conflicting_ids:
        papers.log_merge(paper.id, "same_title", "pending", detail, now)
    return paper.id


def _optional_fields(record: SourceRecord) -> dict[str, str | int | None]:
    return {
        "abstract": record.abstract,
        "venue": record.venue,
        "issn": record.issn,
        "published_at": record.published_at.isoformat() if record.published_at else None,
        "publication_types": (
            json.dumps(record.publication_types, ensure_ascii=False)
            if record.publication_types
            else None
        ),
        "citation_count": record.citation_count,
    }


def _raw_key(source: str, moment: datetime, raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()[:16]
    return f"raw/{source}/{moment:%Y/%m/%d}/{digest}.zst"


class DiscoverStage(Stage):
    name = "discover"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        topics = active_topics(ctx)
        day = ctx.clock().date().isoformat()
        for query in plan_queries(topics.values(), ctx.journal_sets):
            for source in ctx.sources.values():
                if type(query) in source.capabilities:
                    yield JobSpec(
                        key=f"discover:{source.name}:{query.hash}:{day}",
                        stage=self.name,
                        payload={
                            "source": source.name,
                            "query": query.model_dump(mode="json"),
                            "job_key": f"discover:{source.name}:{query.hash}:{day}",
                        },
                    )

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        source = ctx.sources.get(payload["source"])
        if source is None:
            logger.warning("信号源 %s 已停用，跳过待办任务", payload["source"])
            return
        query = query_from_payload(payload["query"])
        cursor = ctx.store.papers.get_cursor(source.name, query.hash)
        scope = scopes(
            "discover", {"job_key": payload.get("job_key")}, ((source.name, query, cursor),)
        )[0]
        result = fetch(ctx, source, query, cursor, scope)
        for warning in result.warnings:
            logger.warning("信号源 %s：%s", source.name, warning)
        moment = ctx.clock()
        raw_key = None
        if result.raw is not None:
            raw_key = _raw_key(source.name, moment, result.raw)
            ctx.blobs.put(raw_key, compress(result.raw))
        now = moment.isoformat()
        with ctx.store.atomic():
            for record in result.records:
                paper_id = resolve_record(ctx.store.papers, record, now)
                ctx.store.papers.record_sighting(paper_id, source.name, query.hash, raw_key, now)
            if isinstance(query, CitationWatch) and result.citations:
                ingest_source_citations(ctx, source.name, result.citations, result.records, now)
            if result.truncated:
                logger.warning("信号源 %s 本次结果被截断，游标保持不变", source.name)
            else:
                ctx.store.papers.set_cursor(source.name, query.hash, result.next_cursor, now)
