"""首次入库只推进本篇论文的既有事实任务，不规划旧文库。"""

import logging

from litgleaner.application.context import AppContext
from litgleaner.application.job_runner import run_stage
from litgleaner.application.stages.base import Stage
from litgleaner.application.stages.extract import (
    ExtractStage,
    extraction_input_hash,
    extraction_producer_version,
)
from litgleaner.application.stages.fulltext import AcquireStage, ParseStage
from litgleaner.domain.errors import WaitingForWork
from litgleaner.ports.store import JobSpec

logger = logging.getLogger(__name__)


def _run(ctx: AppContext, stage: Stage, spec: JobSpec) -> bool:
    run_stage(ctx, stage, specs=(spec,), allowed_keys=frozenset({spec.key}))
    job = ctx.store.jobs.get(spec.key)
    if job is not None and job.status in {"done", "dead"}:
        if job.status == "dead":
            logger.info("首次入库前置已终止 %s：%s", spec.key, job.last_error)
        return job.status == "done"
    raise WaitingForWork(f"首次入库等待本篇 {stage.name} 任务：{spec.key}", 3)


def _acquire(ctx: AppContext, paper_id: str) -> bool:
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is not None:
        return state.status not in {"unavailable", "failed"}
    return _run(
        ctx, AcquireStage(), JobSpec(f"acquire:{paper_id}", "acquire", {"paper_id": paper_id})
    )


def _parse(ctx: AppContext, paper_id: str) -> bool:
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is None or state.status in {"failed", "unavailable"}:
        return False
    if state.doc_key:
        return True
    if not state.pdf_sha256:
        return False
    key = f"parse:{paper_id}:{state.pdf_sha256}:{ctx.parser.parser_id}"
    return _run(ctx, ParseStage(), JobSpec(key, "parse", {"paper_id": paper_id}))


def _extract(ctx: AppContext, paper_id: str) -> bool:
    if ctx.store.papers.get_extraction(paper_id) is not None:
        return True
    state = ctx.store.papers.get_fulltext(paper_id)
    producer = extraction_producer_version(ctx)
    if state is None or not state.doc_key or producer is None:
        return False
    input_hash = extraction_input_hash(
        state.doc_key,
        ctx.settings.chunker,
        ctx.settings.reading_language,
        ctx.settings.assess_token_budget,
    )
    key = f"extract:{paper_id}:{input_hash}:{producer}"
    completed = _run(
        ctx,
        ExtractStage(),
        JobSpec(
            key, "extract", {"paper_id": paper_id, "input_hash": input_hash, "producer": producer}
        ),
    )
    return completed and ctx.store.papers.get_extraction(paper_id) is not None


def prepare_facts(ctx: AppContext, paper_id: str) -> bool:
    """有可核验提取则继续；前置终止或无全文时明确返回缺证据。"""
    return _acquire(ctx, paper_id) and _parse(ctx, paper_id) and _extract(ctx, paper_id)
