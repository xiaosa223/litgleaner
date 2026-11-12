"""显式历史重提取：范围确认、真实请求、缓存幂等及状态隔离。"""

from dataclasses import replace

import pytest

from litgleaner.application import reextract
from litgleaner.application.pipeline import ON_DEMAND_NAMES
from litgleaner.application.stages.extract import ExtractStage
from litgleaner.domain.errors import InvalidRequest, NotFound, PermanentError
from litgleaner.domain.evidence import wrap_untrusted
from tests.e2e.reextract_helpers import QUOTE, change, consume, queued, ready, respond


def test_preview_defaults_to_history_and_explicit_unextracted_and_unknown_are_blocked(harness):
    old = ready(harness)
    untouched = ready(harness, "Never extracted", history=False)
    current = ready(harness, "Current history", current=True)
    plan = reextract.preview(harness.ctx)
    assert {row.paper_id for row in plan.items} == {old, current}
    assert {row.paper_id: row.state for row in plan.items} == {old: "ready", current: "current"}
    assert plan.estimated_usd is None
    selected = reextract.preview(harness.ctx, (untouched, "unknown"))
    assert all(row.state == "blocked" and row.warnings for row in selected.items)
    assert selected.estimated_usd == 0 and harness.llm_calls() == 0


def test_preview_missing_model_is_readonly_and_keeps_unknown_producer_explicit(
    harness, monkeypatch
):
    ready(harness)
    harness.ctx = replace(
        harness.ctx,
        llm=replace(
            harness.ctx.llm,
            routes={
                key: route for key, route in harness.ctx.llm.routes.items() if key != "extract"
            },
        ),
    )
    monkeypatch.setattr(harness.ctx.store._db, "write", lambda *_: pytest.fail("preview write"))
    for provider in harness.ctx.sources.values():
        monkeypatch.setattr(provider, "fetch", lambda *_: pytest.fail("preview fetch"))
    (item,) = reextract.preview(harness.ctx).items
    assert item.state == "blocked" and item.producer_version is None and item.estimated_usd is None
    assert any("模型" in warning for warning in item.warnings)


def test_success_freezes_real_request_records_scope_and_preserves_fulltext_state(
    harness, monkeypatch
):
    id_ = ready(harness, state="failed")
    original = harness.ctx.store.papers.get_fulltext(id_)
    calls = respond(harness, monkeypatch)
    (job,) = queued(harness)
    payload = harness.ctx.store.jobs.get(job.job_key).payload
    assert payload["request_user"] == wrap_untrusted(payload["inputs"])
    assert payload["document_hash"] and payload["source"]["spans"]
    assert payload["scope_id"] == job.job_key and payload["latest_extraction_id"]
    result = consume(harness)
    saved = harness.ctx.store.papers.get_extraction(id_)
    status = reextract.status(harness.ctx, job.job_key)
    assert result.done == 1 and len(calls) == 1 and status.status == "done"
    assert status.result_extraction_id == saved.id and status.cost_usd == 0.125
    assert calls[0].task == "extract" and calls[0].system == payload["system"]
    assert calls[0].user == payload["request_user"] and QUOTE in payload["inputs"]
    assert saved.data["problem"]["verified"] and saved.data["key_concepts"][0]["verified"]
    assert harness.ctx.store.papers.get_fulltext(id_) == original
    assert "reextract" in ON_DEMAND_NAMES and not tuple(ExtractStage().plan(harness.ctx))


def test_repeated_confirm_does_not_depend_on_jobs_or_changed_cost_estimates(harness, monkeypatch):
    ready(harness)
    plan = reextract.preview(harness.ctx)
    first = reextract.queue(harness.ctx, plan.plan_hash)
    monkeypatch.setattr(harness.ctx.store.runs, "average_llm_cost", lambda *_: 0.75)
    again = reextract.preview(harness.ctx)
    assert again.plan_hash == plan.plan_hash and again.estimated_usd == 0.75
    assert reextract.queue(harness.ctx, plan.plan_hash) == first
    assert len(harness.ctx.store.jobs.due("reextract", harness.ctx.now_iso())) == 1


def test_completed_task_reused_and_never_reopened(harness, monkeypatch):
    ready(harness)
    calls = respond(harness, monkeypatch)
    (before,) = queued(harness)
    assert consume(harness).done == 1
    plan = reextract.preview(harness.ctx)
    assert plan.items[0].state == "current" and plan.items[0].estimated_usd == 0
    (after,) = reextract.queue(harness.ctx, plan.plan_hash)
    assert after.job_key == before.job_key and after.status == "done"
    assert consume(harness).done == 0 and len(calls) == 1


def test_v1_v2_v1_keeps_latest_id_and_old_exact_cache_without_repaying(harness, monkeypatch):
    id_ = ready(harness, current=True)
    first = harness.ctx.store.papers.get_extraction(id_)
    original_route = harness.ctx.llm.routes["extract"]
    change(harness, "prompt", id_)
    calls = respond(harness, monkeypatch)
    queued(harness)
    assert consume(harness).done == 1
    latest = harness.ctx.store.papers.get_extraction(id_)
    harness.ctx.llm.routes["extract"] = original_route
    plan = reextract.preview(harness.ctx)
    (item,) = plan.items
    assert item.state == "cached_but_unselected"
    assert item.cached_extraction_id == first.id and item.latest_extraction_id == latest.id
    reextract.queue(harness.ctx, plan.plan_hash)
    assert consume(harness).done == 0 and len(calls) == 1
    assert harness.ctx.store.papers.get_extraction(id_).id == latest.id


def test_complete_scope_change_rejects_confirmation_but_does_not_stop_queued_one(
    harness, monkeypatch
):
    first = ready(harness)
    plan = reextract.preview(harness.ctx)
    ready(harness, "New historical paper")
    with pytest.raises(InvalidRequest, match="重新预览"):
        reextract.queue(harness.ctx, plan.plan_hash)
    queued(harness, (first,))
    ready(harness, "Third historical paper")
    calls = respond(harness, monkeypatch)
    assert consume(harness).done == 1 and len(calls) == 1


def test_preview_reads_each_distinct_archive_once_outside_snapshot_and_failure_isolated(
    harness, monkeypatch
):
    one, two, bad = ready(harness), ready(harness, "Second"), ready(harness, "Bad")
    ctx = harness.ctx
    shared = ctx.store.papers.get_fulltext(one)
    ctx.store.papers.save_fulltext(replace(shared, paper_id=two), ctx.now_iso())
    broken = ctx.store.papers.get_fulltext(bad)
    ctx.blobs.put(broken.doc_key, b"invalid compressed archive")
    get, reads, queries = ctx.blobs.get, [], []
    fetch = ctx.store._db.fetch

    def read(key):
        assert ctx.store._db._active is None
        reads.append(key)
        return get(key)

    def query(statement):
        queries.append(ctx.store._db._active is not None)
        return fetch(statement)

    monkeypatch.setattr(ctx.blobs, "get", read)
    monkeypatch.setattr(ctx.store._db, "fetch", query)
    plan = reextract.preview(ctx)
    assert len(reads) == len(set(reads)) == 2
    assert {row.paper_id: row.state for row in plan.items} == {
        one: "ready",
        two: "ready",
        bad: "blocked",
    }
    assert queries == [True] * 4 + [False]


def test_exact_cache_concurrently_inserted_after_queue_avoids_call_and_concept_side_effects(
    harness, monkeypatch
):
    id_ = ready(harness)
    (job,) = queued(harness)
    p = harness.ctx.store.jobs.get(job.job_key).payload
    harness.ctx.store.papers.insert_extraction(
        id_, p["input_hash"], p["producer_version"], {}, harness.ctx.now_iso()
    )
    calls = respond(harness, monkeypatch)
    monkeypatch.setattr(
        harness.ctx.store.knowledge, "register", lambda *_: pytest.fail("cache concept write")
    )
    assert consume(harness).done == 1 and calls == []
    assert reextract.status(harness.ctx, job.job_key).result_extraction_id


def test_mark_done_failure_after_commit_is_retried_without_repaying(harness, monkeypatch):
    ready(harness)
    (job,) = queued(harness)
    calls = respond(harness, monkeypatch)
    done = harness.ctx.store.jobs.mark_done

    def fail(*_):
        raise RuntimeError("completion acknowledgement failed")

    monkeypatch.setattr(harness.ctx.store.jobs, "mark_done", fail)
    with pytest.raises(RuntimeError, match="acknowledgement"):
        consume(harness)
    monkeypatch.setattr(harness.ctx.store.jobs, "mark_done", done)
    assert consume(harness).done == 1 and len(calls) == 1
    assert reextract.status(harness.ctx, job.job_key).status == "done"


def test_insert_failure_rolls_back_concepts_and_extraction_but_preserves_cost_and_state(
    harness, monkeypatch
):
    id_ = ready(harness)
    state = harness.ctx.store.papers.get_fulltext(id_)
    previous = harness.ctx.store.papers.get_extraction(id_)
    (job,) = queued(harness)
    respond(harness, monkeypatch)

    def fail(*_):
        raise PermanentError("storage failure")

    monkeypatch.setattr(harness.ctx.store.papers, "insert_extraction", fail)
    assert consume(harness).dead == 1
    assert harness.ctx.store.papers.get_extraction(id_) == previous
    assert harness.ctx.store.knowledge.resolve("Transformer") is None
    status = reextract.status(harness.ctx, job.job_key)
    assert (
        status.cost_usd == 0.125
        and status.status == "dead"
        and "storage failure" in status.last_error
    )
    assert harness.ctx.store.papers.get_fulltext(id_) == state


def test_unknown_status_and_empty_ids_are_explicit_errors(harness):
    with pytest.raises(NotFound):
        reextract.status(harness.ctx, "missing")
    with pytest.raises(InvalidRequest):
        reextract.preview(harness.ctx, ("",))


def test_current_natural_key_keeps_cache_id_but_bad_archive_is_blocked(harness):
    id_ = ready(harness, current=True)
    state = harness.ctx.store.papers.get_fulltext(id_)
    harness.ctx.blobs.put(state.doc_key, b"broken archive")
    plan = reextract.preview(harness.ctx)
    (item,) = plan.items
    assert item.state == "blocked" and item.cached_extraction_id == item.latest_extraction_id
    assert any("自然键" in warning for warning in item.warnings)
    assert plan.estimated_usd == 0 and reextract.queue(harness.ctx, plan.plan_hash) == ()


def test_shared_corrupt_archive_failure_is_cached_once_for_whole_preview(harness, monkeypatch):
    one, two = ready(harness), ready(harness, "Same broken archive")
    ctx = harness.ctx
    pointer = ctx.store.papers.get_fulltext(one)
    ctx.store.papers.save_fulltext(replace(pointer, paper_id=two), ctx.now_iso())
    ctx.blobs.put(pointer.doc_key, b"broken archive")
    get, reads = ctx.blobs.get, []

    def counted(key):
        reads.append(key)
        return get(key)

    monkeypatch.setattr(ctx.blobs, "get", counted)
    assert all(row.state == "blocked" for row in reextract.preview(ctx).items)
    assert reads == [pointer.doc_key]


def test_silent_insert_noop_cannot_leave_concept_facts_without_extraction(harness, monkeypatch):
    id_ = ready(harness)
    previous = harness.ctx.store.papers.get_extraction(id_)
    queued(harness)
    respond(harness, monkeypatch)
    monkeypatch.setattr(harness.ctx.store.papers, "insert_extraction", lambda *_: None)
    assert consume(harness).dead == 1
    assert harness.ctx.store.papers.get_extraction(id_) == previous
    assert harness.ctx.store.knowledge.resolve("Transformer") is None
