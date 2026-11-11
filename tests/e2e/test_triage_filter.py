"""初筛前过滤与待处理任务的费用护栏，全部使用本地假适配器。"""

from dataclasses import replace

import pytest
from sqlalchemy import select

from litgleaner.application.stages.triage import TriageStage
from litgleaner.application.topics import (
    activate_topic,
    backfill_topic,
    pause_topic,
    sync_topics,
    triage_job,
)
from litgleaner.domain.keyword_filter import KEYWORD_FILTER_VERSION
from litgleaner.infra.db.schema import triage
from litgleaner.ports.store import JobSpec
from tests.e2e.conftest import Harness, paper


def _rows(harness: Harness):
    return harness.ctx.store._db.fetch(select(triage))


def _prepare(harness: Harness, **fields):
    harness.write_papers([paper("Candidate", **fields)])
    assert harness.run("discover").ok
    specs = tuple(TriageStage().plan(harness.ctx))
    assert len(specs) == 1
    return specs[0]


def test_unrelated_paper_is_rejected_without_a_model_call(harness: Harness):
    harness.write_papers([paper("Ocean salinity", abstract="An unrelated ocean study.")])
    assert harness.run("discover").ok
    report = harness.run("triage")
    assert report.ok
    assert harness.ctx.store.stats()["triage"] == 1
    assert harness.llm_calls("triage") == 0
    result = _rows(harness)[0]
    assert result.relevant is False and result.model == "rule"
    assert result.prompt_version == KEYWORD_FILTER_VERSION and "未命中" in result.reason


def test_excluded_paper_is_saved_by_rule_even_without_a_model_route(harness: Harness):
    harness.edit_yaml("topics/alpha-topic.yaml", lambda d: d["query"].update(exclude=["survey"]))
    harness.rebuild()
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    _prepare(harness, abstract="An alpha SURVEY.")
    report = harness.run("triage")
    assert report.ok and not report.configuration_required
    row = _rows(harness)[0]
    assert row.model == "rule" and not row.relevant and row.reason == "命中排除词：survey"
    assert harness.llm_calls() == 0


def test_synonym_passes_to_the_original_model_and_empty_include_does_not_reject(harness: Harness):
    harness.edit_yaml(
        "topics/alpha-topic.yaml", lambda d: d["query"].update(synonyms={"alpha": ["神经网络"]})
    )
    harness.rebuild()
    spec = _prepare(harness, abstract="卷积神经网络研究。")
    assert spec.payload["topic_hash"] == harness.ctx.topics["alpha-topic"].topic_hash
    assert harness.run("triage").ok and harness.llm_calls("triage") == 1
    assert _rows(harness)[0].model == "cheap" and _rows(harness)[0].relevant

    harness.edit_yaml("topics/alpha-topic.yaml", lambda d: d.update(query={"include": []}))
    harness.rebuild()
    assert backfill_topic(harness.ctx, "alpha-topic", None, False).papers == 1
    assert harness.run("triage").ok and harness.llm_calls("triage") == 2


@pytest.mark.parametrize(
    "abstract", ["Research on alpha.", "Unrelated ocean work.", "Alpha [[irrelevant]]"]
)
def test_duplicate_jobs_skip_existing_positive_or_negative_result(harness: Harness, abstract):
    spec = _prepare(harness, abstract=abstract)
    assert harness.run("triage").ok
    calls = harness.llm_calls("triage")
    harness.ctx.store.jobs.enqueue(
        JobSpec("duplicate", "triage", spec.payload), harness.ctx.now_iso()
    )
    assert harness.run("triage").ok
    assert harness.llm_calls("triage") == calls and len(_rows(harness)) == 1


def test_changed_topic_skips_pending_old_job_then_only_explicit_backfill_uses_new_hash(
    harness: Harness,
):
    spec = _prepare(harness)
    harness.ctx.store.jobs.enqueue(spec, harness.ctx.now_iso())
    harness.clock.advance(minutes=1)
    harness.edit_yaml("topics/alpha-topic.yaml", lambda d: d.update(content="new research focus"))
    harness.rebuild()
    assert harness.run("triage").ok
    assert harness.llm_calls() == 0 and not _rows(harness)
    assert harness.ctx.store.jobs.get(spec.key).status == "done"

    assert backfill_topic(harness.ctx, "alpha-topic", None, True).papers == 1
    assert backfill_topic(harness.ctx, "alpha-topic", None, False).papers == 1
    assert harness.run("triage").ok and harness.llm_calls("triage") == 1
    assert _rows(harness)[0].topic_hash == harness.ctx.topics["alpha-topic"].topic_hash


def test_budget_pending_old_and_new_topic_jobs_call_the_model_once(harness: Harness):
    spec = _prepare(harness)
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, daily_budget_usd=0))
    assert harness.run("triage").budget_stopped
    pending = harness.ctx.store.jobs.get(spec.key)
    assert pending.status == "pending" and pending.attempts == 0
    harness.clock.advance(minutes=1)
    harness.edit_yaml("topics/alpha-topic.yaml", lambda d: d.update(content="new focus"))
    harness.rebuild()
    backfill_topic(harness.ctx, "alpha-topic", None, False)
    assert harness.run("triage").ok
    assert harness.llm_calls("triage") == 1 and len(_rows(harness)) == 1


def test_paused_pending_job_costs_nothing_and_resumes_after_activation(harness: Harness):
    spec = _prepare(harness)
    harness.ctx.store.jobs.enqueue(spec, harness.ctx.now_iso())
    pause_topic(harness.ctx, "alpha-topic")
    assert harness.run("triage").ok
    assert harness.llm_calls() == 0 and not _rows(harness)
    activate_topic(harness.ctx, "alpha-topic")
    assert harness.run("triage").ok
    assert harness.llm_calls("triage") == 1 and len(_rows(harness)) == 1


def test_explicit_backfill_keeps_paused_topics_free_and_can_be_repeated_after_activation(
    harness: Harness,
):
    _prepare(harness)
    pause_topic(harness.ctx, "alpha-topic")
    assert backfill_topic(harness.ctx, "alpha-topic", None, True).papers == 1
    assert backfill_topic(harness.ctx, "alpha-topic", None, False).papers == 1
    assert harness.run("triage").ok and harness.llm_calls() == 0
    activate_topic(harness.ctx, "alpha-topic")
    assert backfill_topic(harness.ctx, "alpha-topic", None, False).papers == 1
    assert harness.run("triage").ok and harness.llm_calls("triage") == 1


def test_legacy_queued_payload_uses_its_current_hash_job_key(harness: Harness):
    spec = _prepare(harness)
    payload = {"paper_id": spec.payload["paper_id"], "topic_id": spec.payload["topic_id"]}
    harness.ctx.store.jobs.enqueue(JobSpec(spec.key, spec.stage, payload), harness.ctx.now_iso())
    assert harness.run("triage").ok and harness.llm_calls("triage") == 1
    assert len(_rows(harness)) == 1


def test_legacy_payload_cannot_borrow_changed_configuration_or_unqueued_authorization(
    harness: Harness,
):
    spec = _prepare(harness)
    payload = {"paper_id": spec.payload["paper_id"], "topic_id": spec.payload["topic_id"]}
    TriageStage().process(harness.ctx, payload)
    assert harness.llm_calls() == 0
    harness.ctx.store.jobs.enqueue(JobSpec(spec.key, spec.stage, payload), harness.ctx.now_iso())
    harness.clock.advance(minutes=1)
    harness.edit_yaml("topics/alpha-topic.yaml", lambda d: d.update(content="changed profile"))
    harness.rebuild()
    assert harness.run("triage").ok
    assert harness.llm_calls() == 0 and not _rows(harness)


def test_removed_topic_pending_task_is_ignored(harness: Harness):
    spec = _prepare(harness)
    harness.ctx.store.jobs.enqueue(spec, harness.ctx.now_iso())
    harness.ctx = replace(harness.ctx, topics={})
    assert harness.run("triage").ok and harness.llm_calls() == 0


def test_legacy_payload_cannot_borrow_new_hash_job_authorization(harness: Harness):
    spec = _prepare(harness)
    payload = {"paper_id": spec.payload["paper_id"], "topic_id": spec.payload["topic_id"]}
    harness.ctx.store.jobs.enqueue(JobSpec(spec.key, spec.stage, payload), harness.ctx.now_iso())
    harness.clock.advance(minutes=1)
    harness.edit_yaml("topics/alpha-topic.yaml", lambda d: d.update(content="new configuration"))
    harness.rebuild()
    sync_topics(harness.ctx)
    current = triage_job(payload["paper_id"], harness.ctx.topics["alpha-topic"])
    harness.ctx.store.jobs.enqueue(current, harness.ctx.now_iso())
    TriageStage().process(harness.ctx, payload)
    assert harness.llm_calls() == 0 and not _rows(harness)
    TriageStage().process(harness.ctx, current.payload)
    assert harness.llm_calls("triage") == 1 and len(_rows(harness)) == 1
