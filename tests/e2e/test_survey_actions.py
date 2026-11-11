"""调研持有论文只限制自动漏斗；用户显式问答和翻译仍按自身授权执行。"""

from dataclasses import replace

import pytest
from sqlalchemy import select

from litgleaner.application import qa, translation_plan
from litgleaner.application.pipeline import run_on_demand
from litgleaner.application.survey import preview, queue, status
from litgleaner.application.topics import pause_topic
from litgleaner.application.translation import cached_translations
from litgleaner.domain.identity import Alias
from litgleaner.infra.db.schema import llm_call
from tests.e2e.conftest import Harness
from tests.e2e.test_survey_flow import ROOT, TOPIC, setup


def _held_parsed_paper(harness: Harness, held_state: str):
    ctx, _ = setup(harness, rounds=1)
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=0.004))
    run = queue(ctx, TOPIC, preview(ctx, TOPIC).id)
    assert run_on_demand(ctx).budget_stopped
    assert harness.llm_calls("triage") == harness.llm_calls("extract") == 2
    assert harness.llm_calls("assess") == 0
    (paper_id,) = ctx.store.papers.find_ids_by_aliases((Alias("doi", ROOT.doi),))
    assert ctx.store.papers.get_fulltext(paper_id).status == "parsed"
    work = ctx.store.surveys.get_work(run.run_id, 1)
    assert ctx.store.runs.llm_cost_for_scope(work.scope_id) == pytest.approx(0.004)
    if held_state == "cancelled":
        ctx = replace(ctx, settings=replace(ctx.settings, survey_sources_version="changed"))
        assert run_on_demand(ctx).ok
        saved = status(ctx, TOPIC, run.run_id)
        assert saved.status == "done" and saved.rounds[0].cancelled
        assert saved.rounds[0].cost_usd == pytest.approx(0.004)
    else:
        pause_topic(ctx, TOPIC)
        assert status(ctx, TOPIC, run.run_id).status == "pending"
    assert paper_id in ctx.store.surveys.held_paper_ids()
    return ctx, paper_id, run.run_id, work.scope_id


def _ledger(ctx, task: str, paper_id: str):
    return ctx.store._db.fetch(
        select(llm_call).where(llm_call.c.task == task, llm_call.c.paper_id == paper_id)
    )


def _assert_action_cost(ctx, task: str, paper_id: str, paper_cost_before: float):
    (call,) = _ledger(ctx, task, paper_id)
    assert (call.input_tokens, call.output_tokens, call.cost_usd, call.ok) == (100, 20, 0.001, True)
    assert call.scope_id is None and call.paper_id == paper_id
    assert ctx.store.runs.llm_cost_for_paper(paper_id) == pytest.approx(paper_cost_before + 0.001)
    assert ctx.store.runs.llm_cost_since("2000-01-01T00:00:00+00:00") == pytest.approx(0.005)


@pytest.mark.parametrize("held_state", ["cancelled", "paused"])
def test_explicit_whole_paper_translation_works_while_survey_holds_paper(
    harness: Harness, held_state: str
):
    ctx, paper_id, run_id, scope_id = _held_parsed_paper(harness, held_state)
    original_rounds = ctx.store.surveys.rounds(TOPIC)
    paper_cost_before = ctx.store.runs.llm_cost_for_paper(paper_id)
    queued = translation_plan.queue(ctx, paper_id)
    assert queued.state == "queued" and queued.pending == queued.paragraphs > 0
    assert queued.estimated_usd is not None and queued.estimated_usd > 0
    assert harness.llm_calls("translate") == 0
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=1))
    report = run_on_demand(ctx)
    translated = next(stage for stage in report.stages if stage.name == "translate")
    assert translated.done == 1 and not translated.errors
    completed = translation_plan.plan(ctx, paper_id)
    assert completed.state == "done" and completed.pending == 0
    assert cached_translations(ctx, paper_id)
    assert harness.llm_calls("translate") == 1
    _assert_action_cost(ctx, "translate", paper_id, paper_cost_before)
    assert ctx.store.surveys.rounds(TOPIC) == original_rounds
    assert ctx.store.runs.llm_cost_for_scope(scope_id) == pytest.approx(0.004)
    assert paper_id in ctx.store.surveys.held_paper_ids()
    assert status(ctx, TOPIC, run_id).status == ("done" if held_state == "cancelled" else "pending")
    run_on_demand(ctx)
    assert harness.llm_calls("translate") == 1
    assert ctx.store.surveys.rounds(TOPIC) == original_rounds


@pytest.mark.parametrize("held_state", ["cancelled", "paused"])
def test_explicit_question_works_while_survey_holds_paper(harness: Harness, held_state: str):
    harness.edit_yaml(
        "llm.yaml",
        lambda data: data["tasks"].update(
            ask={"provider": "main", "model": "strong", "prompt": "ask"}
        ),
    )
    harness.rebuild()
    ctx, paper_id, _, scope_id = _held_parsed_paper(harness, held_state)
    original_rounds = ctx.store.surveys.rounds(TOPIC)
    paper_cost_before = ctx.store.runs.llm_cost_for_paper(paper_id)
    harness.llm.overrides["ask"] = {
        "statements": [
            {
                "text": "The method worked in this setting.",
                "citations": [{"paper_id": paper_id, "page": 1, "quote": "We found it works."}],
            }
        ]
    }
    question = qa.queue(ctx, paper_id, "What worked?", request_id=f"survey-held-{held_state}")
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=1))
    report = run_on_demand(ctx)
    asked = next(stage for stage in report.stages if stage.name == "ask")
    assert asked.done == 1 and not asked.errors
    answer = qa.answer(ctx, question.id)
    assert answer is not None and not answer.statements[0].inference
    assert answer.statements[0].citations[0].quote == "We found it works."
    assert harness.llm_calls("ask") == 1
    _assert_action_cost(ctx, "ask", paper_id, paper_cost_before)
    assert ctx.store.surveys.rounds(TOPIC) == original_rounds
    assert ctx.store.runs.llm_cost_for_scope(scope_id) == pytest.approx(0.004)
    assert paper_id in ctx.store.surveys.held_paper_ids()
    run_on_demand(ctx)
    assert harness.llm_calls("ask") == 1
