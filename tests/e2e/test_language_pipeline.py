"""阅读语言影响生成输入；修改配置只标记失效，历史重评仍需显式操作。"""

import json

import pytest

from litgleaner.application import reader
from litgleaner.application.pipeline import run_stages
from litgleaner.application.rubric_ops import reassess
from litgleaner.application.stages.assess import AssessStage, assessment_input_hash
from litgleaner.application.stages.extract import (
    ExtractStage,
    extraction_input_hash,
    extraction_is_stale,
)
from litgleaner.ports.store import JobSpec
from tests.e2e.conftest import Harness, paper


def _language(harness: Harness, reading: str, ui: str = "zh-CN") -> None:
    harness.edit_yaml("app.yaml", lambda data: data.update(language={"ui": ui, "reading": reading}))
    harness.rebuild()


def _processed(harness: Harness) -> str:
    harness.write_papers([paper("Language Study", 5)])
    assert harness.run().ok
    (paper_id,) = harness.ctx.store.judgments.papers_with_scores()
    return paper_id


@pytest.mark.parametrize("reading,label", [("zh-CN", "简体中文"), ("en", "English")])
def test_pipeline_renders_the_configured_output_language(
    harness: Harness, reading: str, label: str
):
    _language(harness, reading)
    paper_id = _processed(harness)
    calls = {call.task: call for call in harness.llm.calls}
    assert set(calls) == {"triage", "extract", "assess"}
    for task in ("triage", "extract", "assess"):
        assert f"输出语言：{label}" in calls[task].system
        assert "{{output_language}}" not in calls[task].system
    extraction = harness.ctx.store.papers.get_extraction(paper_id)
    assert extraction.data["limitations"][0]["quote"] == "We found it works."
    assert extraction.data["limitations"][0]["verified"] is True


def test_reading_language_is_part_of_both_input_fingerprints(harness: Harness):
    paper_id = _processed(harness)
    state = harness.ctx.store.papers.get_fulltext(paper_id)
    found = harness.ctx.store.papers.get(paper_id)
    topic = harness.ctx.topics["alpha-topic"]
    chunker = harness.ctx.settings.chunker
    assert extraction_input_hash(state.doc_key, chunker, "zh-CN") != extraction_input_hash(
        state.doc_key, chunker, "en"
    )
    assert assessment_input_hash(
        found, topic, state, chunker, reading_language="zh-CN"
    ) != assessment_input_hash(found, topic, state, chunker, reading_language="en")


def test_changing_reading_language_marks_old_results_without_billing_history(harness: Harness):
    paper_id = _processed(harness)
    before = harness.llm_calls()
    extraction = harness.ctx.store.papers.get_extraction(paper_id)
    assert not extraction_is_stale(harness.ctx, paper_id)
    assert reader.paper_view(harness.ctx, paper_id).assessments["alpha-topic"]["stale"] is False

    _language(harness, "en")
    assert harness.run().ok

    assert harness.llm_calls() == before
    assert harness.ctx.store.papers.get_extraction(paper_id) == extraction
    assert extraction_is_stale(harness.ctx, paper_id)
    assert reader.paper_view(harness.ctx, paper_id).assessments["alpha-topic"]["stale"] is True
    archived = json.loads(harness.archive_files()[0].read_text(encoding="utf-8"))
    assert archived["assessments"]["alpha-topic"]["stale"] is True
    assert archived["extraction_stale"] is True


def test_changing_only_ui_language_keeps_input_versions_current(harness: Harness):
    paper_id = _processed(harness)
    before = harness.llm_calls()
    extraction = harness.ctx.store.papers.get_extraction(paper_id)
    old_keys = harness.ctx.store.judgments.assessment_keys(paper_id, "alpha-topic")
    _language(harness, "zh-CN", ui="en")
    assert harness.run().ok
    assert harness.llm_calls() == before
    assert harness.ctx.store.papers.get_extraction(paper_id) == extraction
    assert harness.ctx.store.judgments.assessment_keys(paper_id, "alpha-topic") == old_keys
    assert not extraction_is_stale(harness.ctx, paper_id)
    assert reader.paper_view(harness.ctx, paper_id).assessments["alpha-topic"]["stale"] is False


def test_changed_reading_language_requires_explicit_reassessment(harness: Harness):
    paper_id = _processed(harness)
    before = harness.llm_calls("assess")
    _language(harness, "en")
    plan = reassess(harness.ctx, "alpha-topic", since=None, dry_run=True)
    assert plan.papers == 1 and plan.estimated_usd == pytest.approx(0.001)
    assert harness.llm_calls("assess") == before
    reassess(harness.ctx, "alpha-topic", since=None, dry_run=False)
    assert harness.run().ok
    assert harness.llm_calls("assess") == before + 1
    assert reader.paper_view(harness.ctx, paper_id).assessments["alpha-topic"]["stale"] is False
    latest = [call for call in harness.llm.calls if call.task == "assess"][-1]
    assert "输出语言：English" in latest.system
    assert reassess(harness.ctx, "alpha-topic", since=None, dry_run=True).papers == 0


def test_english_generation_keeps_author_quotes_unchanged_and_verified(harness: Harness):
    _language(harness, "en")
    quote = "We found it works."
    harness.llm.overrides["extract"] = {
        "summary": "A concise study summary.",
        "key_concepts": [],
        "conclusion": "The method works in the reported setting.",
        "innovation": None,
        "limitations": [{"text": "The evidence is limited.", "source": "author", "quote": quote}],
    }
    harness.llm.overrides["assess"] = {
        "dimensions": {
            dim.key: {
                "level": 5,
                "reason": "The paper reports supporting evidence.",
                "quote": quote,
            }
            for dim in harness.ctx.rubrics["default"].dimensions
        },
        "goal_links": [],
    }
    paper_id = _processed(harness)
    extraction = harness.ctx.store.papers.get_extraction(paper_id)
    assert extraction.data["summary"] == "A concise study summary."
    limitation = extraction.data["limitations"][0]
    assert limitation["quote"] == quote and limitation["verified"] is True
    scored = harness.ctx.store.judgments.scored_for_paper(paper_id)[0]
    assert all(
        item["quote"] == quote and item["verified"] for item in scored.assessment.evidence.values()
    )


def test_pending_extraction_uses_only_the_new_language_job_once(harness: Harness):
    harness.write_papers([paper("Pending Language", 5)])
    assert run_stages(harness.ctx, ("discover", "triage", "acquire", "parse")).ok
    (old,) = tuple(ExtractStage().plan(harness.ctx))
    harness.ctx.store.jobs.enqueue(old, harness.ctx.now_iso())  # 模拟预算推迟，尚未执行
    _language(harness, "en")

    assert harness.run("extract").ok

    assert harness.llm_calls("extract") == 1
    assert harness.ctx.store.stats()["extraction"] == 1
    paper_id = old.payload["paper_id"]
    state = harness.ctx.store.papers.get_fulltext(paper_id)
    assert harness.ctx.store.papers.get_extraction(paper_id).input_hash == extraction_input_hash(
        state.doc_key, harness.ctx.settings.chunker, "en"
    )
    assert harness.ctx.store.jobs.get(old.key).status == "done"
    current = next(
        job
        for job in harness.ctx.store.jobs.list_by_status("done")
        if job.stage == "extract" and job.key != old.key
    )
    harness.ctx.store.jobs.enqueue(
        JobSpec("duplicate-extraction", "extract", current.payload), harness.ctx.now_iso()
    )
    assert harness.run("extract").ok
    assert harness.llm_calls("extract") == 1


@pytest.mark.parametrize("change", ["language", "semantic"])
def test_pending_assessment_skips_old_configuration_and_duplicate_jobs(
    harness: Harness, change: str
):
    harness.write_papers([paper("Pending Assessment", 5)])
    assert run_stages(harness.ctx, ("discover", "triage", "acquire", "parse", "extract")).ok
    (old,) = tuple(AssessStage().plan(harness.ctx))
    harness.ctx.store.jobs.enqueue(old, harness.ctx.now_iso())
    if change == "language":
        _language(harness, "en")
    else:
        harness.edit_rubric(lambda data: data["dimensions"][0]["anchors"].update({"3": "新定义"}))
        harness.rebuild()
    assert harness.run("assess").ok
    assert harness.llm_calls("assess") == 1
    assert harness.ctx.store.stats()["assessment"] == 1
    paper_id = old.payload["paper_id"]
    found = harness.ctx.store.papers.get(paper_id)
    state = harness.ctx.store.papers.get_fulltext(paper_id)
    topic = harness.ctx.topics["alpha-topic"]
    (key,) = harness.ctx.store.judgments.assessment_keys(paper_id, topic.id)
    assert key.input_hash == assessment_input_hash(
        found,
        topic,
        state,
        harness.ctx.settings.chunker,
        reading_language=harness.ctx.settings.reading_language,
    )
    assert key.semantic_hash == harness.ctx.rubrics[topic.rubric].semantic_hash
    current = next(
        job
        for job in harness.ctx.store.jobs.list_by_status("done")
        if job.stage == "assess" and job.key != old.key
    )
    harness.ctx.store.jobs.enqueue(
        JobSpec("duplicate-assessment", "assess", current.payload), harness.ctx.now_iso()
    )
    assert harness.run("assess").ok
    assert harness.llm_calls("assess") == 1


def test_legacy_payloads_allow_initial_work_but_never_rebill_existing_results(harness: Harness):
    harness.write_papers([paper("Legacy Payload", 5)])
    assert run_stages(harness.ctx, ("discover", "triage", "acquire", "parse")).ok
    (planned,) = tuple(ExtractStage().plan(harness.ctx))
    payload = {"paper_id": planned.payload["paper_id"]}
    ExtractStage().process(harness.ctx, payload)
    AssessStage().process(harness.ctx, {**payload, "topic_id": "alpha-topic"})
    assert (harness.llm_calls("extract"), harness.llm_calls("assess")) == (1, 1)
    _language(harness, "en")
    ExtractStage().process(harness.ctx, payload)
    AssessStage().process(harness.ctx, {**payload, "topic_id": "alpha-topic"})
    assert (harness.llm_calls("extract"), harness.llm_calls("assess")) == (1, 1)


@pytest.mark.parametrize("stage", ["extract", "assess"])
def test_budget_stopped_jobs_resume_in_new_language_without_duplicate_calls(
    harness: Harness, stage: str
):
    harness.write_papers([paper("Budget Paused Language", 5)])
    setup = ("discover", "triage", "acquire", "parse")
    assert run_stages(harness.ctx, (*setup, "extract") if stage == "assess" else setup).ok
    harness.edit_yaml("llm.yaml", lambda data: data["budget"].update(daily_usd=0))
    harness.rebuild()
    paused = harness.run(stage)
    assert paused.ok and paused.budget_stopped
    pending = [
        job for job in harness.ctx.store.jobs.list_by_status("pending") if job.stage == stage
    ]
    assert len(pending) == 1 and pending[0].attempts == 0
    assert harness.llm_calls(stage) == 0

    harness.edit_yaml("llm.yaml", lambda data: data["budget"].update(daily_usd=10))
    _language(harness, "en")
    assert harness.run(stage).ok
    assert harness.llm_calls(stage) == 1
    assert (
        "输出语言：English" in next(call for call in harness.llm.calls if call.task == stage).system
    )
