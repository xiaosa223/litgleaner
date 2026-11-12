"""真实队列和网关证明空结果幂等、冻结来源、费用及用户决定优先。"""

from dataclasses import replace

import pytest

from litgleaner.application import reader, relate, relation_feedback
from litgleaner.application.documents import load_document, save_document
from litgleaner.application.pipeline import run_on_demand
from litgleaner.application.relate_input import prepare_input
from litgleaner.application.stages.extract import extraction_input_hash, extraction_producer_version
from litgleaner.domain.document import Block
from litgleaner.domain.errors import InvalidRequest, NotFound
from tests.e2e.memory_helpers import add, citation
from tests.e2e.relate_flow_helpers import change_prompt, generate, respond
from tests.e2e.relate_helpers import HERE, THERE, configure, pair, source


def test_real_queue_positive_result_and_fee_scope_are_per_paper(harness, monkeypatch):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    result = generate(harness, a)
    (relation,) = harness.ctx.store.relations.for_paper(a)
    assert relation.paper_a == a and relation.paper_b == b
    assert relation.evidence_a.quote == HERE and relation.evidence_b.quote == THERE
    assert result.cost_usd == 0.01 and harness.llm_calls("relate") == 1
    assert result.result.scope_id == result.job_key
    assert harness.ctx.store.runs.llm_cost_for_paper(a) == 0.01
    assert harness.ctx.store.runs.llm_cost_for_paper(b) == 0
    assert result.result.configuration and result.result.inputs


@pytest.mark.parametrize("mode", ["empty", "unrelated", "bad-quote"])
def test_no_positive_result_still_completes_and_never_repeats_fee(harness, monkeypatch, mode):
    a, _ = pair(harness)
    respond(harness, monkeypatch, mode=mode)
    first = generate(harness, a)
    second = generate(harness, a)
    assert first.result == second.result and first.result.relations == ()
    assert harness.ctx.store.relations.for_paper(a) == ()
    assert harness.llm_calls("relate") == 1 and second.cost_usd == 0.01
    if mode == "bad-quote":
        assert any("核验未通过" in text for text in first.result.warnings)


def test_no_candidates_has_durable_completion_without_model(harness, monkeypatch):
    configure(harness)
    a = add(harness, "only-paper")
    respond(harness, monkeypatch)
    first = generate(harness, a)
    second = generate(harness, a)
    assert first.result == second.result and first.result.input_paper_ids == ()
    assert first.result.relations == () and first.cost_usd == 0
    assert harness.llm_calls("relate") == 0
    assert len(harness.ctx.store.relations.batches_for_paper(a)) == 1


@pytest.mark.parametrize("change", ["prompt", "language", "budget", "source"])
def test_changed_frozen_input_stops_before_any_charge(harness, monkeypatch, change):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    plan = relate.preview(harness.ctx, a)
    queued = relate.queue(harness.ctx, a, plan.plan_hash)
    if change == "prompt":
        change_prompt(harness)
    elif change == "language":
        harness.ctx = replace(
            harness.ctx, settings=replace(harness.ctx.settings, reading_language="en")
        )
    elif change == "budget":
        settings = harness.ctx.settings.relate.model_copy(update={"token_budget": 9000})
        harness.ctx = replace(harness.ctx, settings=replace(harness.ctx.settings, relate=settings))
    else:
        state = harness.ctx.store.papers.get_fulltext(b)
        harness.ctx.store.papers.save_fulltext(
            replace(state, doc_key="missing"), harness.ctx.now_iso()
        )
    run_on_demand(harness.ctx)
    result = relate.status(harness.ctx, a, queued.job_key)
    assert result.status == "dead" and result.result is None and result.last_error
    assert harness.llm_calls("relate") == 0


def test_new_admission_cannot_expand_already_frozen_candidate_pool(harness, monkeypatch):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    plan = relate.preview(harness.ctx, a)
    queued = relate.queue(harness.ctx, a, plan.plan_hash)
    c = add(harness, "newly-admitted")
    source(harness, c, THERE)
    citation(harness, a, target=c)
    assert run_on_demand(harness.ctx).ok
    result = relate.status(harness.ctx, a, queued.job_key)
    assert set(result.result.input_paper_ids) == {a, b} and c not in result.result.inputs
    assert harness.llm_calls("relate") == 1


def test_source_changed_during_paid_call_does_not_write_judgment(harness, monkeypatch):
    a, b = pair(harness)

    def change():
        state = harness.ctx.store.papers.get_fulltext(b)
        harness.ctx.store.papers.save_fulltext(
            replace(state, status="unavailable"), harness.ctx.now_iso()
        )

    # 改变发生在回答构造后，核验后写事务必须重新检查来源。
    respond(harness, monkeypatch)
    original = type(harness.llm).complete

    def complete(self, request):
        result = original(self, request)
        change()
        return result

    monkeypatch.setattr(type(harness.llm), "complete", complete)
    plan = relate.preview(harness.ctx, a)
    queued = relate.queue(harness.ctx, a, plan.plan_hash)
    run_on_demand(harness.ctx)
    result = relate.status(harness.ctx, a, queued.job_key)
    assert result.status == "dead" and result.result is None and result.cost_usd == 0.01
    assert not harness.ctx.store.relations.for_paper(a)


def test_new_valid_judgment_supersedes_but_empty_output_cannot(harness, monkeypatch):
    a, _ = pair(harness)
    respond(harness, monkeypatch)
    first = generate(harness, a)
    old = harness.ctx.store.relations.for_paper(a)[0]
    change_prompt(harness)
    respond(harness, monkeypatch, mode="empty")
    generate(harness, a)
    assert harness.ctx.store.relations.get(old.id).status == "valid"
    change_prompt(harness)
    respond(harness, monkeypatch)
    last = generate(harness, a)
    rows = harness.ctx.store.relations.for_paper(a)
    new = next(row for row in rows if row.status == "valid")
    assert new.batch_id == last.result.id and new.batch_id != first.result.id
    assert harness.ctx.store.relations.get(old.id).superseded_by == new.id


def test_feedback_atomic_idempotence_old_request_does_not_undo_new_decision(harness, monkeypatch):
    a, _ = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    row = harness.ctx.store.relations.for_paper(a)[0]
    relation_feedback.apply(harness.ctx, row.id, "confirm", "first-request")
    rejected = relation_feedback.apply(harness.ctx, row.id, "reject", "second-request")
    replay = relation_feedback.apply(harness.ctx, row.id, "confirm", "first-request")
    assert replay.status == rejected.status == "rejected_by_user"
    with pytest.raises(InvalidRequest, match="请求ID"):
        relation_feedback.apply(harness.ctx, row.id, "reject", "first-request")
    restored = relation_feedback.apply(harness.ctx, row.id, "confirm", "third-request")
    assert restored.status == "valid"


def test_change_appends_user_record_preserves_quotes_and_excludes_pair(harness, monkeypatch):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    old = harness.ctx.store.relations.for_paper(a)[0]
    changed = relation_feedback.apply(
        harness.ctx, old.id, "change", "change-once", kind="same_problem"
    )
    assert changed.source == "user" and changed.kind == "same_problem" and changed.id != old.id
    assert {changed.evidence_a.quote, changed.evidence_b.quote} == {HERE, THERE}
    assert harness.ctx.store.relations.get(old.id).superseded_by == changed.id
    assert (
        relation_feedback.apply(harness.ctx, old.id, "change", "change-once", kind="same_problem")
        == changed
    )
    assert tuple(sorted((a, b))) in harness.ctx.store.relations.user_decided_pairs()
    assert prepare_input(harness.ctx, a).pairs_count == 0
    assert harness.ctx.store.feedback.latest_action(a) is None


def test_feedback_failure_rolls_back_appended_event(harness, monkeypatch):
    a, _ = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    row = harness.ctx.store.relations.for_paper(a)[0]

    def fail(*args, **kwargs):
        raise RuntimeError("feedback persistence failed")

    monkeypatch.setattr(harness.ctx.store.relations, "apply_feedback", fail)
    with pytest.raises(RuntimeError):
        relation_feedback.apply(harness.ctx, row.id, "reject", "must-rollback")
    assert harness.ctx.store.feedback.by_external_id("must-rollback") is None
    assert harness.ctx.store.relations.get(row.id).status == "valid"


def test_status_cannot_read_other_paper_job(harness, monkeypatch):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    result = generate(harness, a)
    with pytest.raises(NotFound):
        relate.status(harness.ctx, b, result.job_key)


def test_first_admission_to_relate_runs_through_actual_on_demand_pipeline(harness, monkeypatch):
    a, b = pair(harness)
    # 真实用户写操作决定先后；同秒也由台账序号确定唯一候选窗口。
    reader.set_reading_state(harness.ctx, b, "reading", None)
    reader.set_reading_state(harness.ctx, a, "reading", None)
    respond(harness, monkeypatch)
    assert run_on_demand(harness.ctx).ok
    assert harness.ctx.store.jobs.get(f"memory_admit:{a}").status == "done"
    assert harness.ctx.store.jobs.get(f"memory_admit:{b}").status == "done"
    (row,) = harness.ctx.store.relations.for_paper(a)
    batch = harness.ctx.store.relations.batch(row.batch_id)
    event = harness.ctx.store.admissions.get(a)
    assert set(batch.input_paper_ids) == {a, b}
    assert batch.scope_id == f"memory_admit:{event.id}"
    assert harness.llm_calls("relate") == 1 and harness.llm_calls("extract") == 0
    assert run_on_demand(harness.ctx).ok and harness.llm_calls("relate") == 1


def test_user_decision_during_paid_call_keeps_batch_without_overriding_feedback(
    harness, monkeypatch
):
    a, _ = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    old = harness.ctx.store.relations.for_paper(a)[0]
    change_prompt(harness)
    respond(
        harness,
        monkeypatch,
        during=lambda: relation_feedback.apply(harness.ctx, old.id, "reject", "during-model-call"),
    )
    result = generate(harness, a)
    assert result.result.relations and harness.llm_calls("relate") == 2
    assert harness.ctx.store.relations.for_paper(a) == (harness.ctx.store.relations.get(old.id),)
    assert harness.ctx.store.relations.get(old.id).status == "rejected_by_user"


def test_task_budget_wait_is_non_attempt_and_resumes_next_day(harness, monkeypatch):
    a, _ = pair(harness)
    route = harness.ctx.llm.routes["relate"]
    harness.ctx = replace(
        harness.ctx,
        llm=replace(
            harness.ctx.llm,
            routes={**harness.ctx.llm.routes, "relate": replace(route, daily_budget_usd=0.01)},
        ),
    )
    respond(harness, monkeypatch)
    generate(harness, a)
    change_prompt(harness)
    plan = relate.preview(harness.ctx, a)
    queued = relate.queue(harness.ctx, a, plan.plan_hash)
    run_on_demand(harness.ctx)
    waiting = harness.ctx.store.jobs.get(queued.job_key)
    assert waiting.status == "pending" and waiting.attempts == 0
    assert harness.llm_calls("relate") == 1
    harness.clock.advance(days=1)
    assert run_on_demand(harness.ctx).ok
    assert relate.status(harness.ctx, a, queued.job_key).status == "done"
    assert harness.llm_calls("relate") == 2


@pytest.mark.parametrize("change", ["evidence", "other-context", "tail"])
def test_paid_call_rechecks_all_sent_context_even_when_document_pointer_is_unchanged(
    harness, monkeypatch, change
):
    a, b = pair(harness)
    extra = "Our second original passage constrains the applicability of this approach."
    if change == "other-context":
        state = harness.ctx.store.papers.get_fulltext(b)
        document = load_document(harness.ctx, state.doc_key)
        updated = replace(
            document,
            parser_id="new-revision",
            blocks=(
                Block("text", THERE, 2),
                Block("text", extra, 3),
            ),
        )
        new_key, _ = save_document(harness.ctx, updated)
        harness.ctx.store.papers.save_fulltext(
            replace(state, doc_key=new_key), harness.ctx.now_iso()
        )
        fields = {
            "problem": {"text": "Problem", "quote": THERE, "page": 2, "verified": True},
            "method": {"text": "Constraint", "quote": extra, "page": 3, "verified": True},
        }
        harness.ctx.store.papers.insert_extraction(
            b,
            extraction_input_hash(
                new_key,
                harness.ctx.settings.chunker,
                harness.ctx.settings.reading_language,
                harness.ctx.settings.assess_token_budget,
            ),
            extraction_producer_version(harness.ctx),
            fields,
            harness.ctx.now_iso(),
        )
    respond(harness, monkeypatch)
    original = type(harness.llm).complete

    def complete(self, request):
        result = original(self, request)
        key = harness.ctx.store.papers.get_fulltext(b).doc_key
        document = load_document(harness.ctx, key)
        text = THERE if change != "evidence" else "This is a different original passage."
        actual, _ = save_document(harness.ctx, replace(document, blocks=(Block("text", text, 2),)))
        assert actual == key
        return result

    monkeypatch.setattr(type(harness.llm), "complete", complete)
    plan = relate.preview(harness.ctx, a)
    queued = relate.queue(harness.ctx, a, plan.plan_hash)
    run_on_demand(harness.ctx)
    result = relate.status(harness.ctx, a, queued.job_key)
    assert result.cost_usd == 0.01
    assert result.status == ("done" if change == "tail" else "dead")
    assert bool(harness.ctx.store.relations.for_paper(a)) == (change == "tail")


def test_changed_price_requires_new_confirmation_without_expiring_cache(harness, monkeypatch):
    a, _ = pair(harness)
    respond(harness, monkeypatch)
    plan = relate.preview(harness.ctx, a)
    price = harness.llm.price("cheap")
    monkeypatch.setattr(
        type(harness.llm), "price", lambda self, model: (price[0] * 2, price[1] * 2)
    )
    changed = relate.preview(harness.ctx, a)
    assert changed.plan_hash != plan.plan_hash and changed.input_hash == plan.input_hash
    assert changed.producer_version == plan.producer_version
    with pytest.raises(InvalidRequest, match="重新预览"):
        relate.queue(harness.ctx, a, plan.plan_hash)
    result = generate(harness, a)
    monkeypatch.setattr(type(harness.llm), "price", lambda self, model: price)
    cached = relate.preview(harness.ctx, a)
    assert cached.cached_batch_id == result.result.id and cached.estimated_usd == 0
