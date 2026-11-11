"""冻结本轮覆盖度和来源贡献；下一轮种子只取本轮有当前评分的论文。"""

from litgleaner.application.context import AppContext
from litgleaner.application.reading_route import _score
from litgleaner.application.survey_jobs import failed
from litgleaner.domain.survey import (
    NewSurveyRound,
    SurveyPreview,
    SurveyRound,
    SurveyWork,
    coverage_ratio,
)


def finish(
    ctx: AppContext, plan: SurveyPreview, work: SurveyWork, *, cancelled: bool = False
) -> SurveyRound:
    known = frozenset(work.paper_ids)
    judgments = ctx.store.judgments
    relevant = known.intersection(judgments.relevant_papers(work.topic_id, work.topic_hash))
    unknown = len(
        known.intersection(judgments.untriaged_papers(work.topic_id, work.topic_hash, None))
    )
    unknown += work.rejected_records
    if cancelled:
        unknown = len(plan.candidates)
    new_passed = len(relevant.intersection(work.new_paper_ids))
    ratio = None if cancelled else coverage_ratio(new_passed, len(plan.candidates), unknown)
    contributions: dict[str, int] = {}
    for paper_id in relevant:
        for source in work.paper_sources[paper_id]:
            contributions[source] = contributions.get(source, 0) + 1
    uncovered = (
        *plan.uncovered,
        *(("配置变化，本轮停止；重新预览后可再调研",) if cancelled else ()),
    )
    return ctx.store.surveys.append_round(
        NewSurveyRound(
            topic_id=work.topic_id,
            topic_hash=work.topic_hash,
            run_id=work.run_id,
            iteration=work.iteration,
            plan_id=plan.id,
            started_at=work.started_at,
            finished_at=ctx.now_iso(),
            candidates=len(plan.candidates),
            new_papers=len(work.new_paper_ids),
            passed=len(relevant),
            new_passed=new_passed,
            undetermined=unknown,
            failed_jobs=failed(ctx, work),
            cost_usd=ctx.store.runs.llm_cost_for_scope(work.scope_id),
            coverage=ratio,
            next_recommended=None if ratio is None else ratio >= plan.settings.coverage_threshold,
            sources=plan.sources,
            contributions=contributions,
            uncovered=uncovered,
            paper_ids=work.paper_ids,
            cancelled=cancelled,
        )
    )


def seeds(ctx: AppContext, result: SurveyRound, limit: int) -> tuple[str, ...]:
    topic = ctx.topics[result.topic_id]
    rubric = ctx.rubrics[topic.rubric]
    snapshot = ctx.store.reading_routes.snapshot(
        topic.id, topic.topic_hash, rubric.semantic_hash, rubric.policy_hash
    )
    ids = frozenset(result.paper_ids)
    ranked: list[tuple[int, str, str]] = []
    for paper in snapshot.papers:
        if paper.id not in ids:
            continue
        score = _score(
            ctx, topic, paper, snapshot.fulltexts.get(paper.id), snapshot.scores.get(paper.id)
        )
        aliases = ctx.store.papers.aliases_of(paper.id)
        doi = next((alias.value for alias in aliases if alias.scheme == "doi"), None)
        if score is not None and doi is not None:
            ranked.append((-score.score, paper.id, doi))
    return tuple(item[2] for item in sorted(ranked)[:limit])
