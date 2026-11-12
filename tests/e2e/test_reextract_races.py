"""付费前后来源与配置竞态不得转为新提取事实。"""

from dataclasses import replace

import pytest
from sqlalchemy import text

from litgleaner.application import reextract
from litgleaner.domain.errors import (
    InvalidRequest,
    PermanentError,
    TransientError,
)
from tests.e2e.reextract_helpers import change, consume, queued, ready, respond

CHANGES = (
    "prompt",
    "model",
    "provider",
    "language",
    "budget",
    "chunker",
    "metadata",
    "body",
    "baseline",
    "pointer",
)


@pytest.mark.parametrize("change_kind", CHANGES)
def test_change_after_preview_requires_fresh_confirmation(harness, change_kind):
    id_ = ready(harness)
    plan = reextract.preview(harness.ctx)
    change(harness, change_kind, id_)
    with pytest.raises(InvalidRequest, match="重新预览"):
        reextract.queue(harness.ctx, plan.plan_hash)
    assert harness.llm_calls() == 0
    assert harness.ctx.store.jobs.due("reextract", harness.ctx.now_iso()) == ()


@pytest.mark.parametrize("change_kind", CHANGES)
def test_change_after_queue_stops_before_any_model_call(harness, monkeypatch, change_kind):
    id_ = ready(harness)
    previous = harness.ctx.store.papers.get_extraction(id_)
    queued(harness)
    change(harness, change_kind, id_)
    calls = respond(harness, monkeypatch)
    assert consume(harness).dead == 1 and calls == [] and harness.llm_calls() == 0
    if change_kind != "baseline":
        assert harness.ctx.store.papers.get_extraction(id_) == previous


@pytest.mark.parametrize("change_kind", CHANGES)
def test_change_during_model_preserves_fee_without_saving_facts(harness, monkeypatch, change_kind):
    id_ = ready(harness)
    previous = harness.ctx.store.papers.get_extraction(id_)
    (job,) = queued(harness)
    calls = respond(harness, monkeypatch, hook=lambda: change(harness, change_kind, id_))
    assert consume(harness).dead == 1 and len(calls) == 1
    if change_kind != "baseline":
        assert harness.ctx.store.papers.get_extraction(id_) == previous
    status = reextract.status(harness.ctx, job.job_key)
    assert status.result_extraction_id is None and status.cost_usd == 0.125


@pytest.mark.parametrize("error", [TransientError("temporary"), PermanentError("permanent")])
def test_model_failure_does_not_demote_fulltext_or_replace_old_result(harness, monkeypatch, error):
    id_ = ready(harness)
    original = harness.ctx.store.papers.get_fulltext(id_)
    previous = harness.ctx.store.papers.get_extraction(id_)
    queued(harness)
    respond(harness, monkeypatch, error=error)
    result = consume(harness)
    assert result.dead == isinstance(error, PermanentError)
    assert result.retried == isinstance(error, TransientError)
    assert harness.ctx.store.papers.get_fulltext(id_) == original
    assert harness.ctx.store.papers.get_extraction(id_) == previous


def test_global_budget_leaves_pending_and_task_budget_defers_without_attempt(harness, monkeypatch):
    ready(harness)
    (job,) = queued(harness)
    calls = respond(harness, monkeypatch)
    object.__setattr__(harness.ctx.llm, "daily_budget_usd", 0.0)
    assert consume(harness).budget_stopped and calls == []
    assert harness.ctx.store.jobs.get(job.job_key).status == "pending"
    object.__setattr__(harness.ctx.llm, "daily_budget_usd", 10.0)
    route = harness.ctx.llm.routes["extract"]
    harness.ctx.llm.routes["extract"] = replace(route, daily_budget_usd=0.0)
    result = consume(harness)
    saved = harness.ctx.store.jobs.get(job.job_key)
    assert result.retried == 1 and saved.attempts == 0 and saved.first_deferred_at is None
    assert calls == [] and "预算" in saved.last_error


def test_archive_rereads_never_hold_write_lock(harness, monkeypatch):
    id_ = ready(harness)
    calls = respond(harness, monkeypatch)
    queued(harness)
    get, reads = harness.ctx.blobs.get, []

    def guarded(key):
        assert harness.ctx.store._db._active is None
        reads.append(key)
        return get(key)

    monkeypatch.setattr(harness.ctx.blobs, "get", guarded)
    assert consume(harness).done == 1 and len(calls) == 1 and len(reads) == 2
    assert harness.ctx.store.papers.get_extraction(id_).data["summary"] == "fresh summary"


@pytest.mark.parametrize("change_kind", ["metadata", "prompt"])
def test_final_write_lock_rechecks_revision_and_producer_after_archive_refresh(
    harness, monkeypatch, change_kind
):
    id_ = ready(harness)
    previous = harness.ctx.store.papers.get_extraction(id_)
    (job,) = queued(harness)
    respond(harness, monkeypatch)
    revision = harness.ctx.store.reextract_revision
    checks = []

    def racing(ids):
        checks.append(ids)
        if len(checks) == 2:
            assert harness.ctx.store._db._active is not None
            change(harness, change_kind, id_)
        return revision(ids)

    monkeypatch.setattr(harness.ctx.store, "reextract_revision", racing)
    assert consume(harness).dead == 1 and len(checks) == 2
    assert harness.ctx.store.papers.get_extraction(id_) == previous
    assert harness.ctx.store.knowledge.resolve("Transformer") is None
    assert reextract.status(harness.ctx, job.job_key).cost_usd == 0.125


def test_corrupted_scope_cannot_transfer_charges_to_another_job(harness, monkeypatch):
    ready(harness)
    (job,) = queued(harness)
    harness.ctx.store._db.write(
        text(
            "UPDATE job SET payload=json_set(payload,'$.scope_id','other-scope') WHERE key=:key"
        ).bindparams(key=job.job_key)
    )
    calls = respond(harness, monkeypatch)
    assert consume(harness).dead == 1 and calls == []
    assert reextract.status(harness.ctx, job.job_key).cost_usd == 0
