"""归档阶段：JSON 文件是数据库的导出结果，永远不手改，也不反向写入。"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.stages.base import Stage
from litgleaner.domain.archive import archive_key, build_archive_record
from litgleaner.domain.paper import Paper
from litgleaner.ports.store import JobSpec, ScoredAssessment


def _entry(scored: ScoredAssessment, stale: bool) -> dict[str, Any]:
    assessment = scored.assessment
    return {
        "score": scored.score,
        "tier": scored.tier,
        "provisional": scored.provisional,
        "undetermined": list(scored.undetermined),
        "fulltext_used": assessment.fulltext_used,
        "levels": dict(assessment.levels),
        "evidence": dict(assessment.evidence),
        "semantic_hash": assessment.semantic_hash,
        "policy_hash": scored.policy_hash,
        "stale": stale,
    }


def _topic_entries(ctx: AppContext, paper_id: str) -> tuple[dict[str, dict[str, Any]], bool]:
    """当前标准下的结果优先。改了维度定义但没重评时，保留旧结果并标记 stale，不删归档。"""
    entries: dict[str, dict[str, Any]] = {}
    kept = False
    rows = ctx.store.judgments.scored_for_paper(paper_id)
    for topic_id in sorted({row.assessment.topic_id for row in rows}):
        topic = ctx.topics.get(topic_id)
        rubric = ctx.rubrics.get(topic.rubric) if topic else None
        own = [row for row in rows if row.assessment.topic_id == topic_id]
        current = [
            row
            for row in own
            if rubric
            and row.assessment.semantic_hash == rubric.semantic_hash
            and row.policy_hash == rubric.policy_hash
        ]
        chosen = current[-1] if current else own[-1]
        entries[topic_id] = _entry(chosen, stale=not current)
        fallback = rubric.fallback_tier if rubric else None
        kept = kept or chosen.tier != fallback
    return entries, kept


def _record(ctx: AppContext, paper: Paper) -> tuple[dict[str, Any], bool]:
    entries, kept = _topic_entries(ctx, paper.id)
    extraction = ctx.store.papers.get_extraction(paper.id)
    aliases = {alias.scheme: alias.value for alias in ctx.store.papers.aliases_of(paper.id)}
    fulltext = ctx.store.papers.get_fulltext(paper.id)
    record = build_archive_record(
        paper,
        aliases,
        extraction.data if extraction else None,
        entries,
        fulltext_used=bool(fulltext and fulltext.status == "parsed"),
    )
    return record, kept


def _desired(ctx: AppContext, paper: Paper) -> bytes | None:
    """这篇论文的归档文件应有的内容。None 表示不应该有文件。"""
    record, kept = _record(ctx, paper)
    if not kept:
        return None
    text = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return text.encode("utf-8")


def write_archive(ctx: AppContext, paper_id: str) -> None:
    paper = ctx.store.papers.get(paper_id)
    if paper is None:
        raise ValueError(f"论文不存在：{paper_id}")
    key = archive_key(paper)
    desired = _desired(ctx, paper)
    if desired is not None:
        ctx.archive.put(key, desired)
    elif ctx.archive.exists(key):
        ctx.archive.delete(key)


class ArchiveStage(Stage):
    name = "archive"
    reopen_when_planned = True

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        for paper_id in ctx.store.judgments.papers_with_scores():
            paper = ctx.store.papers.get(paper_id)
            if paper is None:
                continue
            key = archive_key(paper)
            current = ctx.archive.get(key) if ctx.archive.exists(key) else None
            if _desired(ctx, paper) != current:
                yield JobSpec(f"archive:{paper_id}", self.name, {"paper_id": paper_id})

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        write_archive(ctx, payload["paper_id"])
