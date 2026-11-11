"""真实队列调研：两轮、恢复、配置护栏与费用归属；全部来源和模型离线。"""

from dataclasses import replace

import pytest

from litgleaner.application.pipeline import run_on_demand, run_pipeline
from litgleaner.application.stages.discover import resolve_record
from litgleaner.application.stages.fulltext import AcquireStage, ParseStage
from litgleaner.application.survey import preview, queue, status
from litgleaner.application.topics import activate_topic, pause_topic, triage_job
from litgleaner.domain.citation import ReferenceRecord
from litgleaner.domain.errors import AuthFailed, InvalidRequest, TransientError
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import HistoricalCitations, HistoricalSearch, ReferenceList
from litgleaner.domain.survey import SurveySettings
from litgleaner.ports.source import FetchResult
from litgleaner.ports.store import TriageResult
from tests.e2e.conftest import Harness, paper

TOPIC = "alpha-topic"
ROOT = SourceRecord(**paper("High alpha [[level:5]]", 5))
LOW = SourceRecord(**paper("Low alpha [[level:1]]", 1))
LOCAL = SourceRecord(title="unrelated mathematics", doi="10.1000/local")
NEW = SourceRecord(**paper("Expansion alpha [[level:4]]", 4))
CITING = SourceRecord(**paper("Citing alpha NoPDF", 3))


class SurveySource:
    name = "offline"
    capabilities = frozenset({HistoricalSearch, HistoricalCitations, ReferenceList})

    def __init__(self):
        self.queries = []
        self.history = (ROOT, LOW, LOCAL)

    def fetch(self, query, cursor):
        assert cursor is None
        self.queries.append(query)
        if isinstance(query, HistoricalSearch):
            return FetchResult(self.history, None, raw=b"history")
        if isinstance(query, ReferenceList):
            assert query.dois == (ROOT.doi,)
            refs = tuple(
                ReferenceRecord(
                    ROOT.doi, n, record.title, record.title, (Alias("doi", record.doi),)
                )
                for n, record in enumerate((ROOT, NEW))
            )
            return FetchResult((ROOT, NEW), None, references=refs)
        assert isinstance(query, HistoricalCitations) and query.dois == (ROOT.doi,)
        return FetchResult((CITING,), None)


def setup(harness: Harness, rounds=2):
    source = SurveySource()
    ctx = replace(
        harness.ctx,
        sources={"offline": source},
        settings=replace(harness.ctx.settings, survey=SurveySettings(rounds=rounds, seed_limit=1)),
    )
    harness.ctx = ctx
    return ctx, source


def test_preview_is_persistent_zero_model_and_confirm_executes_two_real_rounds(harness: Harness):
    ctx, source = setup(harness)
    plan = preview(ctx, TOPIC)
    assert len(plan.candidates) == 3 and plan.triage_calls == 2 and plan.locally_rejected == 1
    assert plan.estimated_usd is None and plan.estimated_pipeline_usd is None
    assert ctx.store.stats()["paper"] == harness.llm_calls() == 0
    assert all(isinstance(query, HistoricalSearch) for query in source.queries)
    run = queue(ctx, TOPIC, plan.id)
    assert len(source.queries) == 3 and harness.llm_calls() == 0
    report = run_on_demand(ctx)
    assert report.ok, report.summary()
    result = status(ctx, TOPIC, run.run_id)
    assert result.status == "done" and len(result.rounds) == 2
    first, second = result.rounds
    assert (first.candidates, first.new_papers, first.passed, first.new_passed) == (3, 3, 2, 2)
    assert first.coverage == pytest.approx(2 / 3)
    assert (second.candidates, second.new_papers, second.passed, second.new_passed) == (3, 2, 3, 2)
    assert second.coverage == pytest.approx(2 / 3)
    assert first.cost_usd == pytest.approx(0.006)
    assert second.cost_usd == pytest.approx(0.005)
    assert first.contributions == {"offline": 2} and second.contributions == {"offline": 3}
    assert len(source.queries) == 5
    assert all(query.dois == (ROOT.doi,) for query in source.queries[3:])
    assert ctx.store.stats()["paper"] == 5 and ctx.store.stats()["assessment"] == 4
    assert harness.publisher().sent == [] and harness.archive_files() == []
    before = harness.llm_calls()
    assert queue(ctx, TOPIC, plan.id).run_id == run.run_id
    assert run_on_demand(ctx).ok and harness.llm_calls() == before
    for receipt in plan.sources:
        assert ctx.store.papers.get_cursor(receipt.source, receipt.query_hash) is None


def test_existing_data_is_reused_and_new_means_missing_at_confirm_not_preview(harness: Harness):
    ctx, _ = setup(harness, rounds=1)
    plan = preview(ctx, TOPIC)
    with ctx.store.atomic():
        resolve_record(ctx.store.papers, ROOT, ctx.now_iso())
    run = queue(ctx, TOPIC, plan.id)
    assert run_on_demand(ctx).ok
    first = status(ctx, TOPIC, run.run_id).rounds[0]
    assert first.new_papers == 2 and first.new_passed == 1
    assert first.coverage == pytest.approx(1 / 3)
    before = harness.llm_calls()
    rerun = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert run_on_demand(ctx).ok
    result = status(ctx, TOPIC, rerun.run_id).rounds[0]
    assert result.new_papers == result.new_passed == result.cost_usd == 0
    assert result.coverage == 0 and result.next_recommended is False
    assert harness.llm_calls() == before


@pytest.mark.parametrize(
    "change", ["topic", "model", "language", "source", "budget_input", "settings"]
)
def test_stale_preview_cannot_charge(harness: Harness, change):
    ctx, _ = setup(harness, rounds=1)
    plan = preview(ctx, TOPIC)
    if change == "topic":
        topic = ctx.topics[TOPIC].model_copy(update={"background": "changed"})
        ctx = replace(ctx, topics={TOPIC: topic})
    elif change == "model":
        routes = dict(ctx.llm.routes)
        routes["triage"] = replace(routes["triage"], model="changed")
        ctx = replace(ctx, llm=replace(ctx.llm, routes=routes))
    else:
        fields = {
            "language": {"reading_language": "en"},
            "source": {"survey_sources_version": "new"},
            "budget_input": {"assess_token_budget": 42},
            "settings": {"survey": SurveySettings(rounds=2)},
        }
        ctx = replace(ctx, settings=replace(ctx.settings, **fields[change]))
    with pytest.raises(InvalidRequest, match="重新预览"):
        queue(ctx, TOPIC, plan.id)
    assert harness.llm_calls() == 0 and ctx.store.stats()["paper"] == 0


def test_pause_waits_without_failure_then_resumes(harness: Harness):
    ctx, _ = setup(harness, rounds=1)
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    pause_topic(ctx, TOPIC)
    report = run_on_demand(ctx)
    assert next(stage for stage in report.stages if stage.name == "survey").retried == 1
    assert harness.llm_calls() == 0
    assert ctx.store.jobs.get(f"survey:{run.run_id}").attempts == 0
    harness.clock.advance(days=4)
    report = run_on_demand(ctx)
    assert next(stage for stage in report.stages if stage.name == "survey").retried == 1
    activate_topic(ctx, TOPIC)
    harness.clock.advance(minutes=1)
    assert run_on_demand(ctx).ok
    assert status(ctx, TOPIC, run.run_id).status == "done"


def test_daily_budget_resumes_without_duplicate_paid_calls(harness: Harness):
    ctx, _ = setup(harness, rounds=1)
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=0.001))
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert run_on_demand(ctx).budget_stopped
    assert harness.llm_calls() == 1
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=1))
    assert run_on_demand(ctx).ok
    result = status(ctx, TOPIC, run.run_id).rounds[0]
    assert result.cost_usd == pytest.approx(0.006)
    assert harness.llm_calls("triage") == 2


def test_transient_child_retries_once_and_unrelated_work_is_not_processed(harness: Harness):
    ctx, _ = setup(harness, rounds=1)
    harness.llm.fail_tasks["triage"] = [TransientError("offline transient")]
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert not run_on_demand(ctx).ok
    assert not status(ctx, TOPIC, run.run_id).rounds
    harness.clock.advance(minutes=6)
    assert run_on_demand(ctx).ok
    result = status(ctx, TOPIC, run.run_id).rounds[0]
    assert not result.failed_jobs and result.cost_usd == pytest.approx(0.006)


def test_dead_triage_keeps_coverage_unknown(harness: Harness):
    ctx, _ = setup(harness, rounds=1)
    harness.llm.fail_tasks["triage"] = [AuthFailed("offline invalid token")]
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert run_on_demand(ctx).ok
    result = status(ctx, TOPIC, run.run_id).rounds[0]
    assert result.undetermined == 1 and result.coverage is None
    assert result.next_recommended is None and len(result.failed_jobs) == 1


def test_config_change_mid_run_cancels_without_using_new_model(harness: Harness):
    ctx, _ = setup(harness, rounds=1)
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=0.001))
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert run_on_demand(ctx).budget_stopped
    ctx = replace(ctx, settings=replace(ctx.settings, reading_language="en"))
    assert run_on_demand(ctx).ok
    result = status(ctx, TOPIC, run.run_id).rounds[0]
    assert result.cancelled and result.coverage is None and result.cost_usd == pytest.approx(0.001)
    assert harness.llm_calls() == 1


def test_daily_pipeline_cannot_steal_waiting_survey_papers_or_existing_jobs(harness: Harness):
    ctx, _ = setup(harness, rounds=1)
    harness.llm.fail_tasks["triage"] = [TransientError("offline transient")]
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert not run_pipeline(ctx).ok
    assert len(harness.llm.calls) == 2 and harness.llm_calls() == 1
    assert harness.llm_calls("extract") == harness.llm_calls("assess") == 0
    work = ctx.store.surveys.get_work(run.run_id, 1)
    assert set(work.paper_ids) <= ctx.store.surveys.held_paper_ids()
    pending = next(
        job for job in ctx.store.jobs.list_by_status("pending") if job.stage == "survey_child"
    )
    daily = triage_job(pending.payload["paper_id"], ctx.topics[TOPIC])
    ctx.store.jobs.enqueue(daily, ctx.now_iso())
    before = harness.llm_calls()
    run_pipeline(ctx)
    assert harness.llm_calls() == before
    assert ctx.store.jobs.get(daily.key).status == "pending"
    harness.clock.advance(minutes=6)
    assert run_pipeline(ctx).ok
    result = status(ctx, TOPIC, run.run_id).rounds[0]
    assert result.cost_usd == pytest.approx(0.006)
    assert ctx.store.runs.llm_cost_since("2000-01-01T00:00:00+00:00") == pytest.approx(0.006)
    assert not ctx.store.surveys.held_paper_ids()


def test_daily_budget_resume_keeps_every_call_in_survey_scope(harness: Harness):
    ctx, _ = setup(harness, rounds=1)
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=0.001))
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert run_pipeline(ctx).budget_stopped
    assert harness.llm_calls() == 1
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=1))
    assert run_pipeline(ctx).ok
    result = status(ctx, TOPIC, run.run_id).rounds[0]
    assert result.cost_usd == pytest.approx(0.006)
    assert ctx.store.runs.llm_cost_since("2000-01-01T00:00:00+00:00") == pytest.approx(0.006)


def test_cancelled_pending_children_finish_and_daily_cannot_resume_them(harness: Harness):
    ctx, _ = setup(harness, rounds=1)
    harness.llm.fail_tasks["triage"] = [TransientError("offline transient")]
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert not run_on_demand(ctx).ok
    before = harness.llm_calls()
    ctx = replace(ctx, settings=replace(ctx.settings, reading_language="en"))
    harness.clock.advance(minutes=6)
    assert run_pipeline(ctx).ok
    assert status(ctx, TOPIC, run.run_id).rounds[0].cancelled
    assert harness.llm_calls() == before
    assert not any(job.stage == "survey_child" for job in ctx.store.jobs.list_by_status("pending"))
    assert ctx.store.surveys.held_paper_ids()
    assert ctx.store.jobs.retry_dead(ctx.now_iso()) == 0
    assert run_pipeline(ctx).ok and harness.llm_calls() == before


def test_terminal_failed_children_cannot_reopen_and_new_preview_can_recover(harness: Harness):
    ctx, _ = setup(harness, rounds=1)
    harness.llm.fail_tasks["triage"] = [AuthFailed("offline invalid")]
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert run_pipeline(ctx).ok
    original = status(ctx, TOPIC, run.run_id).rounds[0]
    assert original.undetermined == 1 and original.coverage is None
    before = harness.llm_calls()
    assert ctx.store.jobs.retry_dead(ctx.now_iso()) == 0
    assert run_pipeline(ctx).ok and harness.llm_calls() == before
    assert not any(job.stage == "survey_child" for job in ctx.store.jobs.list_by_status("pending"))
    second = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert run_pipeline(ctx).ok
    recovered = status(ctx, TOPIC, second.run_id).rounds[0]
    assert recovered.passed == 2 and recovered.undetermined == 0
    assert not ctx.store.surveys.held_paper_ids()
    assert status(ctx, TOPIC, run.run_id).rounds[0] == original


def test_preparsed_rejected_paper_does_not_trigger_extraction(harness: Harness):
    ctx, source = setup(harness, rounds=1)
    source.history = (LOCAL,)
    with ctx.store.atomic():
        id_ = resolve_record(ctx.store.papers, LOCAL, ctx.now_iso())
        ctx.store.judgments.save_triage(
            TriageResult(id_, TOPIC, ctx.topics[TOPIC].topic_hash, False, "reject", "fake", "fake"),
            ctx.now_iso(),
        )
    AcquireStage().process(ctx, {"paper_id": id_})
    ParseStage().process(ctx, {"paper_id": id_})
    assert ctx.store.papers.get_fulltext(id_).status == "parsed"
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert run_on_demand(ctx).ok
    assert status(ctx, TOPIC, run.run_id).rounds[0].cost_usd == 0
    assert ctx.store.papers.get_extraction(id_) is None and harness.llm_calls() == 0


def test_internal_expansion_plan_cannot_be_confirmed_as_separate_run(harness: Harness):
    ctx, _ = setup(harness)
    plan = preview(ctx, TOPIC)
    run = queue(ctx, TOPIC, plan.id)
    assert run_on_demand(ctx).ok
    second = status(ctx, TOPIC, run.run_id).rounds[1]
    with pytest.raises(InvalidRequest, match="自动轮次"):
        queue(ctx, TOPIC, second.plan_id)
    assert queue(ctx, TOPIC).run_id == run.run_id


def test_alias_conflict_is_not_assigned_to_an_arbitrary_paper(harness: Harness):
    ctx, source = setup(harness, rounds=1)
    left = SourceRecord(title="Left alpha", doi="10.1000/left")
    right = SourceRecord(title="Right alpha", external_ids={"manual": "right"})
    with ctx.store.atomic():
        left_id = resolve_record(ctx.store.papers, left, ctx.now_iso())
        right_id = resolve_record(ctx.store.papers, right, ctx.now_iso())
    source.history = (
        SourceRecord(title="Conflict alpha", doi=left.doi, external_ids={"manual": "right"}),
    )
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert run_on_demand(ctx).ok
    result = status(ctx, TOPIC, run.run_id).rounds[0]
    assert result.paper_ids == () and result.undetermined == 1 and result.coverage is None
    assert result.cost_usd == harness.llm_calls() == 0
    assert ctx.store.papers.get(left_id).title == left.title
    assert ctx.store.papers.get(right_id).title == right.title
