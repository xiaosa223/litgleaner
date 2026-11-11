"""确认后一次入库；冲突记录不猜测归属，引用保留来源事实。"""

from litgleaner.application.citations import exact_match
from litgleaner.application.context import AppContext
from litgleaner.application.stages.discover import resolve_record
from litgleaner.application.survey_plan import resolution
from litgleaner.domain.citation import Citation
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.survey import SurveyPreview, SurveyWork


def prepare(ctx: AppContext, plan: SurveyPreview, run_id: str, iteration: int) -> SurveyWork:
    with ctx.store.atomic():
        if saved := ctx.store.surveys.get_work(run_id, iteration):
            return saved
        ctx.store.surveys.accept(plan.id, run_id, ctx.now_iso())
        papers: dict[str, set[str]] = {}
        new: list[str] = []
        rejected = 0
        for candidate in plan.candidates:
            match = resolution(ctx, candidate.record)
            if match.kind == "conflict":
                rejected += 1
                continue
            paper_id = resolve_record(ctx.store.papers, candidate.record, ctx.now_iso())
            if match.kind == "new":
                new.append(paper_id)
            papers.setdefault(paper_id, set()).update(candidate.sources)
            for source in candidate.sources:
                ctx.store.papers.record_sighting(
                    paper_id, source, f"survey:{plan.id}", None, ctx.now_iso()
                )
        _edges(ctx, plan)
        work = SurveyWork(
            run_id=run_id,
            iteration=iteration,
            plan_id=plan.id,
            scope_id=f"survey:{run_id}:{iteration}",
            topic_id=plan.topic_id,
            topic_hash=plan.topic_hash,
            started_at=ctx.now_iso(),
            paper_ids=tuple(papers),
            new_paper_ids=tuple(new),
            paper_sources={id_: tuple(sorted(v)) for id_, v in papers.items()},
            rejected_records=rejected,
        )
        ctx.store.surveys.save_work(work)
        return work


def _edges(ctx: AppContext, plan: SurveyPreview) -> None:
    for edge in plan.edges:
        citing = exact_match(ctx, edge.citing_aliases)
        if citing is None:
            continue
        ctx.store.citations.add(
            Citation(
                citing,
                f"survey:{edge.source}:{stable_hash(edge.model_dump(mode='json'))}",
                edge.raw_text,
                edge.source,
                edge.source_position,
                exact_match(ctx, edge.cited_aliases),
                None,
                edge.cited_aliases,
                edge.contexts,
                edge.intents,
                edge.influential,
                ctx.now_iso(),
            )
        )
