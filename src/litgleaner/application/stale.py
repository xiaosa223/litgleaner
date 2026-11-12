"""统一只读报告，不同步课题、不重建归档、不排队任何模型工作。"""

from litgleaner.application.context import AppContext
from litgleaner.application.stale_embeddings import check_embeddings
from litgleaner.application.stale_facts import (
    check_assessments,
    check_extractions,
    check_scores,
)
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.application.stale_language import check_answers, check_translations
from litgleaner.application.stale_relations import check_relations
from litgleaner.application.stale_topics import check_maps, check_outlines
from litgleaner.domain.stale import StaleReport


def report(ctx: AppContext) -> StaleReport:
    basis = StaleInputs(ctx, ctx.store.stale_snapshot())
    items = tuple(
        sorted(
            (
                *check_extractions(basis),
                *check_assessments(basis),
                *check_scores(basis),
                *check_translations(basis),
                *check_answers(basis),
                *check_maps(basis),
                *check_outlines(basis),
                *check_embeddings(basis),
                *check_relations(basis),
            ),
            key=lambda entry: (entry.kind, entry.topic_id or "", entry.paper_id or "", entry.id),
        )
    )
    return StaleReport(
        items,
        sum(entry.status == "current" for entry in items),
        sum(entry.status == "stale" for entry in items),
        sum(entry.status == "unknown" for entry in items),
    )
