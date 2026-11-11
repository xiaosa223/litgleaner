"""按实际历史复用条件估算首轮漏斗；未知后续轮次按候选限额计。"""

from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.stages.assess import _needs_assessment
from litgleaner.application.survey_plan import resolution
from litgleaner.domain.keyword_filter import filter_keywords
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.survey import SurveySettings
from litgleaner.domain.topic import Topic


@dataclass(frozen=True)
class SurveyCounts:
    triage_calls: int = 0
    fulltext_candidates: int = 0
    extract_calls: int = 0
    assess_calls: int = 0
    locally_rejected: int = 0
    known_papers: int = 0


def _downstream(ctx: AppContext, topic: Topic, paper_id: str | None) -> tuple[int, int, int]:
    state = ctx.store.papers.get_fulltext(paper_id) if paper_id else None
    fulltext = (
        state is None
        or state.status == "available"
        or (state.status == "parsed" and state.doc_key is not None)
    )
    extraction = ctx.store.papers.get_extraction(paper_id) if paper_id else None
    keys = ctx.store.judgments.assessment_keys(paper_id, topic.id) if paper_id else ()
    assess = _needs_assessment(
        keys, ctx.rubrics[topic.rubric].semantic_hash, topic.topic_hash, fulltext
    )
    return int(fulltext), int(fulltext and extraction is None), int(assess)


def counts(ctx: AppContext, topic: Topic, records: tuple[SourceRecord, ...]) -> SurveyCounts:
    """假设待初筛候选均相关、未知全文可取得，仍复用已存在的历史结果。"""
    untriaged = frozenset(ctx.store.judgments.untriaged_papers(topic.id, topic.topic_hash, None))
    relevant = frozenset(ctx.store.judgments.relevant_papers(topic.id, topic.topic_hash))
    triage = fulltext = extract = assess = rejected = known = 0
    visited: set[str] = set()
    for record in records:
        match = resolution(ctx, record)
        if match.kind == "conflict" or match.paper_id in visited:
            continue
        paper_id = match.paper_id
        if paper_id:
            visited.add(paper_id)
            known += 1
        needs_triage = paper_id is None or paper_id in untriaged
        if not needs_triage and paper_id not in relevant:
            continue
        paper = ctx.store.papers.get(paper_id) if paper_id else None
        title, abstract = (
            (paper.title, paper.abstract or record.abstract)
            if paper
            else (record.title, record.abstract)
        )
        if not filter_keywords(title, abstract, topic.query).passed:
            rejected += 1
            continue
        triage += needs_triage
        expected_fulltext, extract_calls, assess_calls = _downstream(ctx, topic, paper_id)
        fulltext += expected_fulltext
        extract += extract_calls
        assess += assess_calls
    return SurveyCounts(triage, fulltext, extract, assess, rejected, known)


def _cost(ctx: AppContext, task: str, calls: int) -> float | None:
    if calls == 0:
        return 0.0
    mean = ctx.store.runs.average_llm_cost(task)
    return None if mean is None else calls * mean


def estimates(
    ctx: AppContext, settings: SurveySettings, planned: SurveyCounts
) -> tuple[float | None, float | None]:
    """每个模型任务单独计数；有调用但无历史均价时保留未知。"""
    later = (settings.rounds - 1) * settings.candidate_limit
    initial = _cost(ctx, "triage", planned.triage_calls + later)
    costs = (
        initial,
        _cost(ctx, "extract", planned.extract_calls + later),
        _cost(ctx, "assess", planned.assess_calls + later),
    )
    pipeline = (
        None
        if any(cost is None for cost in costs)
        else sum(cost for cost in costs if cost is not None)
    )
    return initial, pipeline
