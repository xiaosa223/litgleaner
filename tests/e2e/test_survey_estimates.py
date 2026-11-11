"""预览按阶段复用实际历史，费用计算全离线且不产生模型调用。"""

from dataclasses import replace

import pytest
from pydantic import ValidationError

from litgleaner.application.stages.discover import resolve_record
from litgleaner.application.survey import preview
from litgleaner.application.survey_estimate import SurveyCounts, counts, estimates
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import HistoricalSearch
from litgleaner.domain.survey import SurveyPreview, SurveySettings
from litgleaner.ports.source import FetchResult
from litgleaner.ports.store import FullTextState, LLMCall, NewAssessment, TriageResult
from tests.e2e.conftest import Harness, paper

TOPIC = "alpha-topic"
RECORD = SourceRecord(**paper("Relevant alpha research"))


def add_paper(harness: Harness, *, relevant: bool | None = True, record=RECORD) -> str:
    ctx, topic = harness.ctx, harness.ctx.topics[TOPIC]
    paper_id = resolve_record(ctx.store.papers, record, ctx.now_iso())
    if relevant is not None:
        ctx.store.judgments.save_triage(
            TriageResult(paper_id, TOPIC, topic.topic_hash, relevant, "Past result", "v1", "old"),
            ctx.now_iso(),
        )
    return paper_id


def assessment(
    harness: Harness, paper_id: str, *, fulltext=True, old_semantic=False, old_topic=False
) -> None:
    ctx, topic = harness.ctx, harness.ctx.topics[TOPIC]
    ctx.store.judgments.insert_assessment(
        NewAssessment(
            paper_id,
            TOPIC,
            "old-topic" if old_topic else topic.topic_hash,
            "old-semantic" if old_semantic else ctx.rubrics[topic.rubric].semantic_hash,
            "old-input",
            {},
            {},
            fulltext,
            "old-model",
            "old-prompt",
        ),
        ctx.now_iso(),
    )


def history(harness: Harness, **means: float) -> None:
    for task, cost in means.items():
        harness.ctx.store.runs.record_llm_call(
            LLMCall(task, "recorded-model", "old", 20, 20, cost, True), harness.ctx.now_iso()
        )


class OfflineSource:
    capabilities = frozenset({HistoricalSearch})
    name = "offline"

    def fetch(self, query, cursor):
        return FetchResult((RECORD,), None)


def test_preview_of_relevant_unassessed_paper_has_downstream_cost_with_zero_triage(
    harness: Harness,
):
    ctx = harness.ctx
    add_paper(harness)
    history(harness, extract=0.02, assess=0.04)
    ctx = replace(
        ctx,
        sources={"offline": OfflineSource()},
        settings=replace(ctx.settings, survey=SurveySettings(rounds=1)),
    )
    before = harness.llm_calls()
    planned = preview(ctx, TOPIC)
    assert (
        planned.triage_calls,
        planned.fulltext_candidates,
        planned.extract_calls,
        planned.assess_calls,
    ) == (0, 1, 1, 1)
    assert planned.estimated_usd == 0 and planned.estimated_pipeline_usd == pytest.approx(0.06)
    assert ctx.store.surveys.preview(planned.id) == planned
    assert harness.llm_calls() == before and ctx.store.stats()["paper"] == 1


def test_fully_reusable_paper_estimates_zero_without_any_price_history(harness: Harness):
    ctx = harness.ctx
    paper_id = add_paper(harness)
    ctx.store.papers.save_fulltext(
        FullTextState(paper_id, "parsed", doc_key="old-doc"), ctx.now_iso()
    )
    ctx.store.papers.insert_extraction(paper_id, "old-input", "old-prompt", {}, ctx.now_iso())
    assessment(harness, paper_id)
    ctx = replace(
        ctx,
        sources={"offline": OfflineSource()},
        settings=replace(ctx.settings, survey=SurveySettings(rounds=1)),
    )
    planned = preview(ctx, TOPIC)
    assert planned.triage_calls == planned.extract_calls == planned.assess_calls == 0
    assert planned.fulltext_candidates == 1 and planned.known_papers == 1
    assert planned.estimated_usd == planned.estimated_pipeline_usd == 0
    assert harness.llm_calls() == 0 and not ctx.store.jobs.list_by_status("pending")


@pytest.mark.parametrize(
    "status, extracted, judged, expected",
    [
        (None, False, None, (1, 1, 1)),
        ("available", False, None, (1, 1, 1)),
        ("parsed", False, None, (1, 1, 1)),
        ("parsed", True, None, (1, 0, 1)),
        ("parsed", True, "fulltext", (1, 0, 0)),
        ("parsed", True, "abstract", (1, 0, 1)),
        ("unavailable", False, None, (0, 0, 1)),
        ("failed", False, None, (0, 0, 1)),
        ("unavailable", False, "abstract", (0, 0, 0)),
        ("parsed", True, "old-semantic", (1, 0, 0)),
        ("parsed", True, "old-topic", (1, 0, 1)),
    ],
)
def test_per_stage_counts_follow_fulltext_and_history_reuse(
    harness: Harness, status, extracted, judged, expected
):
    ctx, topic = harness.ctx, harness.ctx.topics[TOPIC]
    paper_id = add_paper(harness)
    if status:
        ctx.store.papers.save_fulltext(
            FullTextState(paper_id, status, doc_key="old-document" if status == "parsed" else None),
            ctx.now_iso(),
        )
    if extracted:
        ctx.store.papers.insert_extraction(paper_id, "old-input", "old-version", {}, ctx.now_iso())
    if judged:
        assessment(
            harness,
            paper_id,
            fulltext=judged != "abstract",
            old_semantic=judged == "old-semantic",
            old_topic=judged == "old-topic",
        )
    planned = counts(ctx, topic, (RECORD,))
    assert (planned.fulltext_candidates, planned.extract_calls, planned.assess_calls) == expected
    assert planned.triage_calls == planned.locally_rejected == 0 and planned.known_papers == 1
    assert harness.llm_calls() == 0


def test_new_and_untriaged_candidates_assume_relevance_after_keyword_pass(harness: Harness):
    ctx, topic = harness.ctx, harness.ctx.topics[TOPIC]
    planned = counts(ctx, topic, (RECORD,))
    assert planned == SurveyCounts(1, 1, 1, 1, 0, 0)
    add_paper(harness, relevant=None)
    assert counts(ctx, topic, (RECORD,)) == SurveyCounts(1, 1, 1, 1, 0, 1)
    assert harness.llm_calls() == 0


@pytest.mark.parametrize("relevant", [None, False, True])
def test_keyword_rejection_never_predicts_downstream_calls(harness: Harness, relevant):
    ctx, topic = harness.ctx, harness.ctx.topics[TOPIC]
    record = SourceRecord(title="Unrelated mathematics", doi="10.1000/rejected")
    add_paper(harness, record=record, relevant=relevant)
    planned = counts(ctx, topic, (record,))
    assert (
        planned.triage_calls
        == planned.fulltext_candidates
        == planned.extract_calls
        == planned.assess_calls
        == 0
    )
    assert planned.known_papers == 1 and planned.locally_rejected == (0 if relevant is False else 1)


def test_prior_negative_triage_and_identity_conflict_do_not_generate_cost(harness: Harness):
    ctx, topic = harness.ctx, harness.ctx.topics[TOPIC]
    paper_id = add_paper(harness, relevant=False)
    ctx.store.papers.save_fulltext(
        FullTextState(paper_id, "parsed", doc_key="old-doc"), ctx.now_iso()
    )
    assert ctx.store.papers.get_extraction(paper_id) is None
    assert counts(ctx, topic, (RECORD,)) == SurveyCounts(known_papers=1)
    extra = SourceRecord(title="Separate alpha paper", arxiv_id="2601.00001")
    resolve_record(ctx.store.papers, extra, ctx.now_iso())
    conflict = RECORD.model_copy(update={"arxiv_id": extra.arxiv_id})
    assert counts(ctx, topic, (conflict,)) == SurveyCounts()


@pytest.mark.parametrize(
    "planned, means, expected",
    [
        (SurveyCounts(), {}, (0, 0)),
        (SurveyCounts(triage_calls=2), {"triage": 0.1}, (0.2, 0.2)),
        (SurveyCounts(extract_calls=1), {}, (0, None)),
        (SurveyCounts(assess_calls=1), {}, (0, None)),
        (SurveyCounts(extract_calls=1), {"extract": 0.3}, (0, 0.3)),
        (SurveyCounts(triage_calls=1), {"extract": 0.3, "assess": 0.5}, (None, None)),
        (SurveyCounts(assess_calls=2), {"assess": 0}, (0, 0)),
    ],
)
def test_zero_call_tasks_need_no_history_and_positive_call_tasks_keep_unknown(
    harness: Harness, planned, means, expected
):
    history(harness, **means)
    assert estimates(harness.ctx, SurveySettings(rounds=1), planned) == expected


def test_each_task_has_its_own_first_round_plus_later_candidate_cap(harness: Harness):
    history(harness, triage=0.01, extract=0.02, assess=0.04)
    planned = SurveyCounts(triage_calls=1, extract_calls=2, assess_calls=3)
    initial, pipeline = estimates(harness.ctx, SurveySettings(rounds=3, candidate_limit=5), planned)
    assert initial == pytest.approx(11 * 0.01)
    assert pipeline == pytest.approx(11 * 0.01 + 12 * 0.02 + 13 * 0.04)


def test_future_rounds_need_history_even_when_first_round_is_fully_reusable(harness: Harness):
    assert estimates(harness.ctx, SurveySettings(rounds=2), SurveyCounts()) == (None, None)


def minimal_preview(**changes):
    return SurveyPreview(
        **{
            "id": "plan",
            "topic_id": TOPIC,
            "topic_hash": "hash",
            "fingerprint": "fp",
            "created_at": "now",
            "settings": SurveySettings(),
            "candidates": (),
            "triage_calls": 0,
            "locally_rejected": 0,
            "known_papers": 0,
            **changes,
        }
    )


@pytest.mark.parametrize("field", ["fulltext_candidates", "extract_calls", "assess_calls"])
@pytest.mark.parametrize("bad", [-1, True, "1", 1.0])
def test_preview_stage_counts_are_strict_nonnegative_integers(field, bad):
    with pytest.raises(ValidationError):
        minimal_preview(**{field: bad})


def test_legacy_preview_without_new_counts_still_loads():
    planned = minimal_preview()
    assert planned.fulltext_candidates == planned.extract_calls == planned.assess_calls == 0


def test_preview_passes_all_seeds_to_collection_for_explicit_limit_gap(harness: Harness):
    ctx = replace(
        harness.ctx,
        sources={},
        settings=replace(harness.ctx.settings, survey=SurveySettings(rounds=1, seed_limit=1)),
    )
    planned = preview(ctx, TOPIC, dois=("10.1000/a", "10.1000/b"), historical=False)
    assert any("seed_limit=1" in gap for gap in planned.uncovered)
    assert harness.llm_calls() == 0 and ctx.store.stats()["paper"] == 0
