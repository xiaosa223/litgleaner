"""课题限定的对比表与引用导出，只投影本地事实和当前判断。"""

from dataclasses import dataclass
from typing import Literal

from litgleaner.application.context import AppContext
from litgleaner.application.stages.assess import assessment_input_hash
from litgleaner.application.stages.extract import (
    extraction_input_hash,
    extraction_producer_version,
)
from litgleaner.domain.comparison_table import ComparisonRow, render_table
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.export_references import ReferenceExport, render_references
from litgleaner.domain.paper import Paper
from litgleaner.domain.topic import Topic
from litgleaner.ports.export import TopicExportQuery, TopicExportSnapshot
from litgleaner.ports.store import Extraction, FullTextState, ScoredAssessment

TableFormat = Literal["md", "csv"]
ReferenceFormat = Literal["bibtex", "csl-json"]


@dataclass(frozen=True)
class TopicComparison:
    topic_id: str
    topic_hash: str
    total: int
    tiers: tuple[str, ...]
    rows: tuple[ComparisonRow, ...]


@dataclass(frozen=True)
class TopicReferences:
    topic_id: str
    topic_hash: str
    total: int
    only_read: bool
    references: ReferenceExport
    needs_verification: tuple[Paper, ...]


def _topic(ctx: AppContext, topic_id: str) -> Topic:
    topic = ctx.topics.get(topic_id)
    if topic is None:
        raise NotFound(f"课题不存在：{topic_id}")
    return topic


def _snapshot(ctx: AppContext, topic: Topic) -> TopicExportSnapshot:
    rubric = ctx.rubrics[topic.rubric]
    return ctx.store.topic_export_snapshot(
        TopicExportQuery(topic.id, topic.topic_hash, rubric.semantic_hash, rubric.policy_hash)
    )


def _tiers(ctx: AppContext, topic: Topic, value: str | None) -> tuple[str, ...]:
    if value is None:
        return ()
    parts = tuple(dict.fromkeys(part.strip() for part in value.split(",")))
    rubric = ctx.rubrics[topic.rubric]
    allowed = {tier.key for tier in rubric.tiers} | {rubric.provisional.tier}
    if not parts or any(not part or part not in allowed for part in parts):
        raise InvalidRequest("等级筛选无效；可选值：" + ", ".join(sorted(allowed)))
    return parts


def _extraction_stale(
    ctx: AppContext, extraction: Extraction | None, state: FullTextState | None
) -> bool:
    if extraction is None:
        return False
    if state is None or state.doc_key is None:
        return True
    expected = extraction_input_hash(
        state.doc_key,
        ctx.settings.chunker,
        ctx.settings.reading_language,
        ctx.settings.assess_token_budget,
    )
    producer = extraction_producer_version(ctx)
    return extraction.input_hash != expected or (
        producer is not None and extraction.extractor_version != producer
    )


def _current_score(
    ctx: AppContext,
    topic: Topic,
    paper: Paper,
    state: FullTextState | None,
    score: ScoredAssessment | None,
) -> ScoredAssessment | None:
    if state is None or score is None:
        return None
    expected = assessment_input_hash(
        paper,
        topic,
        state,
        ctx.settings.chunker,
        reading_language=ctx.settings.reading_language,
        token_budget=ctx.settings.assess_token_budget,
    )
    return score if score.assessment.input_hash == expected else None


def _row(
    ctx: AppContext, topic: Topic, paper: Paper, snapshot: TopicExportSnapshot
) -> ComparisonRow:
    state = snapshot.fulltexts.get(paper.id)
    extraction = snapshot.extractions.get(paper.id)
    saved = snapshot.scores.get(paper.id)
    current = _current_score(ctx, topic, paper, state, saved)
    return ComparisonRow(
        paper_id=paper.id,
        title=paper.title,
        extraction=extraction.data if extraction else None,
        extraction_stale=_extraction_stale(ctx, extraction, state),
        reading_value=current.score if current else None,
        tier=current.tier if current else None,
        score_stale=saved is not None and current is None,
        provisional=current.provisional if current else None,
        goal_links=current.assessment.goal_links if current else (),
        goals={goal.id: goal.text for goal in topic.goals},
    )


def comparison(ctx: AppContext, topic_id: str, *, tier: str | None = None) -> TopicComparison:
    """暂停课题也能导出；不补抽事实，不重评，不读文档 blob。"""
    topic = _topic(ctx, topic_id)
    tiers = _tiers(ctx, topic, tier)
    snapshot = _snapshot(ctx, topic)
    rows = tuple(_row(ctx, topic, paper, snapshot) for paper in snapshot.papers)
    selected = tuple(row for row in rows if not tiers or row.tier in tiers)
    return TopicComparison(topic.id, topic.topic_hash, len(rows), tiers, selected)


def references(ctx: AppContext, topic_id: str, *, only_read: bool = False) -> TopicReferences:
    topic = _topic(ctx, topic_id)
    snapshot = _snapshot(ctx, topic)
    papers = tuple(
        paper
        for paper in snapshot.papers
        if not only_read
        or ((reading := snapshot.reading.get(paper.id)) is not None and reading.status == "read")
    )
    rendered = render_references(
        tuple((paper, snapshot.aliases.get(paper.id, ())) for paper in papers)
    )
    missing = frozenset(rendered.unverified_ids)
    return TopicReferences(
        topic.id,
        topic.topic_hash,
        len(papers),
        only_read,
        rendered,
        tuple(paper for paper in papers if paper.id in missing),
    )


def table_text(result: TopicComparison, format: TableFormat) -> str:
    if format not in ("md", "csv"):
        raise InvalidRequest("对比表格式必须是 md 或 csv")
    return render_table(result.rows, format)


def reference_text(result: TopicReferences, format: ReferenceFormat) -> str:
    if format not in ("bibtex", "csl-json"):
        raise InvalidRequest("引用格式必须是 bibtex 或 csl-json")
    return result.references.bibtex if format == "bibtex" else result.references.csl_json
