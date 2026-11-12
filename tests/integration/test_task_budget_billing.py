"""真实费用账本的任务隔离、历史时区和失败用量，不接触外部模型。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from sqlalchemy import select, text

from litgleaner.application.llm_gateway import LLMGateway, LLMOutputInvalid, Prompt, TaskRoute
from litgleaner.domain.errors import BudgetExceeded, ModelUsage, TaskBudgetExceeded, TransientError
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import llm_call
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.llm import LLM, LLMResponse, StreamDone, TextDelta
from litgleaner.ports.store import LLMCall


@pytest.fixture
def billing(tmp_path):
    url = f"sqlite:///{tmp_path / 'task-billing.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    now = [datetime(2026, 11, 11, 19, tzinfo=UTC)]
    llm = Mock(spec=LLM)
    llm.complete.return_value = LLMResponse({}, "reported-model", 10, 5, 0.2)
    routes = {
        name: TaskRoute(llm, "requested-model", Prompt(name, "prompt"), daily_budget_usd=0.2)
        for name in ("ask", "translate")
    }
    subject = LLMGateway(routes, store.runs, 10, lambda: now[0])
    yield subject, llm, store, now
    store.dispose()


def complete(gateway, task="ask", **kwargs):
    return gateway.call(task, {}, "paper", {"type": "object"}, **kwargs)


def record(store, task, cost, when, **kwargs):
    store.runs.record_llm_call(LLMCall(task, "m", "v", 1, 1, cost, True, **kwargs), when)


def test_task_limits_are_independent_but_global_budget_remains_shared(billing):
    subject, llm, store, _now = billing
    complete(subject)
    with pytest.raises(TaskBudgetExceeded):
        complete(subject)
    complete(subject, "translate")
    assert llm.complete.call_count == 2
    assert store.runs.llm_cost_since("2026-11-11", task="ask") == 0.2
    assert store.runs.llm_cost_since("2026-11-11", task="translate") == 0.2
    assert store.runs.llm_cost_since("2026-11-11") == 0.4
    with pytest.raises(BudgetExceeded):
        complete(replace(subject, daily_budget_usd=0.4))
    assert store.runs.llm_call_count() == 2


def test_changing_provider_model_or_scope_does_not_reset_task_spend(billing):
    subject, llm, store, _now = billing
    complete(replace(subject, scope_id="old-run"), paper_id="P")
    other = Mock(spec=LLM)
    route = TaskRoute(
        other, "new-model", Prompt("ask", "new prompt"), "new-provider", daily_budget_usd=0.2
    )
    changed = replace(subject, routes={"ask": route}, scope_id="new-run")
    with pytest.raises(TaskBudgetExceeded):
        complete(changed, paper_id="Q")
    assert llm.complete.call_count == 1
    other.complete.assert_not_called()
    assert store.runs.llm_cost_for_scope("old-run") == 0.2
    assert store.runs.llm_cost_for_scope("new-run") == 0
    assert store.runs.llm_cost_for_paper("P") == 0.2
    assert store.runs.llm_cost_for_paper("Q") == 0


@pytest.mark.parametrize("failure", ["schema", "known-usage", "unknown-usage"])
def test_failed_completion_consumes_only_reported_task_usage(billing, failure):
    subject, llm, store, _now = billing
    schema = {"type": "object", "required": ["missing"]}
    if failure != "schema":
        error = TransientError("completion failed")
        if failure == "known-usage":
            error.model_usage = ModelUsage("failed-response-model", 8, 4, 0.2)
        llm.complete.side_effect = error
    with pytest.raises((LLMOutputInvalid, TransientError)):
        subject.call("ask", {}, "paper", schema)
    expected = 0 if failure == "unknown-usage" else 0.2
    assert store.runs.llm_cost_since("2026-11-11", task="ask") == expected
    assert all(row.ok is False for row in store._db.fetch(select(llm_call)))
    llm.complete.side_effect = None
    if expected:
        with pytest.raises(TaskBudgetExceeded):
            complete(subject)
        assert llm.complete.call_count == 1
    else:
        complete(subject)
        assert llm.complete.call_count == 2


@pytest.mark.parametrize("failure", [False, True])
def test_stream_success_or_reported_failure_consumes_task_budget_before_next_stream(
    billing, failure
):
    subject, llm, store, _now = billing
    error = TransientError("stream failed") if failure else None

    def events(_request):
        yield TextDelta("partial")
        yield StreamDone("stream-model", 8, 4, 0.2, error=error)
        if error is not None:
            raise error

    llm.stream.side_effect = events
    stream = subject.stream("translate", {}, "paper", paper_id="P")
    if failure:
        with pytest.raises(TransientError):
            list(stream)
    else:
        assert list(stream) == ["partial"]
    with pytest.raises(TaskBudgetExceeded):
        subject.stream("translate", {}, "paper", paper_id="P")
    assert llm.stream.call_count == 1
    (row,) = store._db.fetch(select(llm_call))
    assert row.ok is not failure
    assert store.runs.llm_cost_for_paper("P") == 0.2
    assert store.runs.llm_cost_since("2026-11-11", task="translate") == 0.2
    complete(subject)


def test_single_call_may_exceed_remaining_task_balance_then_future_calls_pause(billing):
    subject, llm, store, _now = billing
    smaller = replace(subject.require_route("ask"), daily_budget_usd=0.1)
    subject = replace(subject, routes={"ask": smaller})
    complete(subject)
    with pytest.raises(TaskBudgetExceeded):
        complete(subject)
    assert llm.complete.call_count == 1
    assert store.runs.llm_cost_since("2026-11-11", task="ask") == 0.2


def test_non_utc_clock_records_utc_and_budget_resets_at_utc_day_boundary(billing):
    subject, llm, store, now = billing
    local = timezone(timedelta(hours=8))
    now[0] = datetime(2026, 11, 11, 7, 59, tzinfo=local)
    complete(subject)
    with pytest.raises(TaskBudgetExceeded) as caught:
        complete(subject)
    assert caught.value.retry_after_seconds == 60
    now[0] += timedelta(minutes=1)
    complete(subject)
    rows = store._db.fetch(select(llm_call).order_by(llm_call.c.id))
    assert [row.created_at for row in rows] == [
        "2026-11-10T23:59:00+00:00",
        "2026-11-11T00:00:00+00:00",
    ]
    assert llm.complete.call_count == 2
    assert store.runs.llm_cost_since("2026-11-11", task="ask") == 0.2


@pytest.mark.parametrize(
    ("when", "included"),
    [
        ("2026-11-11T00:00:00+08:00", False),
        ("2026-11-11T07:59:59.999999+08:00", False),
        ("2026-11-10T23:59:59.999999+00:00", False),
        ("2026-11-10T18:59:59.999999-05:00", False),
        ("2026-11-10T19:00:00-05:00", True),
        ("2026-11-11T08:00:00+08:00", True),
        ("2026-11-11T00:00:00+00:00", True),
        ("2026-11-11T00:00:00Z", True),
        ("2026-11-11T00:00:00.000001+00:00", True),
        ("2026-11-11T00:00:00", True),
        ("2026-11-11", True),
        ("2026-11-11T00:00:00+00:00:00.000001", False),
        ("2026-11-10T23:59:59.999999-00:00:00.000001", True),
    ],
)
def test_historical_offsets_and_microseconds_follow_actual_utc_day(billing, when, included):
    _subject, _llm, store, _now = billing
    record(store, "ask", 0.2, when)
    record(store, "translate", 0.3, when)
    for since in ("2026-11-11", "2026-11-11T08:00:00+08:00"):
        assert store.runs.llm_cost_since(since) == (0.5 if included else 0)
        assert store.runs.llm_cost_since(since, task="ask") == (0.2 if included else 0)
        assert store.runs.llm_cost_since(since, task="translate") == (0.3 if included else 0)
    assert store.runs.llm_cost_since("2026-11-11", task="missing") == 0
    assert store.runs.llm_cost_since("2026-11-11", task="") == 0


def test_cost_query_preserves_fractional_second_boundaries(billing):
    _subject, _llm, store, _now = billing
    record(store, "ask", 0.2, "2026-11-11T00:00:00.499999+00:00")
    record(store, "ask", 0.3, "2026-11-11T08:00:00.5+08:00")
    record(store, "ask", 0.4, "2026-11-10T19:00:00.500001-05:00")
    since = "2026-11-11T00:00:00.500000+00:00"
    assert store.runs.llm_cost_since(since) == pytest.approx(0.7)
    assert store.runs.llm_cost_since(since, task="ask") == pytest.approx(0.7)


def test_historical_configuration_probe_cost_counts_only_toward_global_budget(billing):
    subject, llm, store, _now = billing
    record(store, "configuration_test", 0.2, "2026-11-10T19:00:00-05:00")
    complete(subject)
    assert llm.complete.call_count == 1
    assert store.runs.llm_cost_since("2026-11-11", task="ask") == 0.2
    assert store.runs.llm_cost_since("2026-11-11") == 0.4


def test_task_filter_is_a_bound_literal_not_an_sql_fragment(billing):
    _subject, _llm, store, _now = billing
    crafted = "ask' OR 1=1 --"
    record(store, "ask", 0.2, "2026-11-11T00:00:00+00:00")
    record(store, crafted, 0.3, "2026-11-11T00:00:00+00:00")
    assert store.runs.llm_cost_since("2026-11-11", task=crafted) == 0.3
    assert store.runs.llm_call_count() == 2


def stream_events(_request):
    yield TextDelta("answer")
    yield StreamDone("stream-model", 8, 4, 0.2)


def limited(gateway, limit):
    if limit == "global":
        route = replace(gateway.require_route("translate"), daily_budget_usd=None)
        return replace(gateway, routes={**gateway.routes, "translate": route}, daily_budget_usd=0.2)
    return gateway


@pytest.mark.parametrize("limit", ["global", "task"])
@pytest.mark.parametrize("reuse", ["same", "prebuilt"])
def test_stream_cannot_reuse_preflight_to_bypass_a_subsequently_exhausted_budget(
    billing, limit, reuse
):
    subject, llm, store, _now = billing
    subject = limited(subject, limit)
    llm.stream.side_effect = stream_events
    first = subject.stream("translate", {}, "paper")
    second = first if reuse == "same" else subject.stream("translate", {}, "paper")
    assert list(first) == ["answer"]
    error = BudgetExceeded if limit == "global" else TaskBudgetExceeded
    with pytest.raises(error):
        list(second)
    assert llm.stream.call_count == 1
    assert store.runs.llm_call_count("translate") == 1


@pytest.mark.parametrize("limit", ["global", "task"])
@pytest.mark.parametrize("cross_day", [False, True])
def test_delayed_stream_checks_current_usage_and_current_utc_day(billing, limit, cross_day):
    subject, llm, store, now = billing
    subject = limited(subject, limit)
    now[0] = datetime(2026, 11, 11, 23, 59, tzinfo=UTC)
    stream = subject.stream("translate", {}, "paper")
    if cross_day:
        now[0] += timedelta(minutes=1)
    complete(subject, "translate")
    error = BudgetExceeded if limit == "global" else TaskBudgetExceeded
    with pytest.raises(error):
        list(stream)
    llm.stream.assert_not_called()
    assert store.runs.llm_call_count("translate") == 1


@pytest.mark.parametrize("limit", ["global", "task"])
def test_delayed_stream_uses_new_day_balance_instead_of_preflight_day(billing, limit):
    subject, llm, store, now = billing
    subject = limited(subject, limit)
    llm.stream.side_effect = stream_events
    now[0] = datetime(2026, 11, 11, 23, 59, tzinfo=UTC)
    stream = subject.stream("translate", {}, "paper")
    complete(subject, "translate")
    now[0] += timedelta(minutes=1)
    assert list(stream) == ["answer"]
    assert llm.stream.call_count == 1
    assert store.runs.llm_call_count("translate") == 2


EXTREME_OFFSETS = (
    timedelta(hours=23, minutes=59),
    -timedelta(hours=23, minutes=59),
    timedelta(hours=23, minutes=59, seconds=59, microseconds=999999),
    -timedelta(hours=23, minutes=59, seconds=59, microseconds=999999),
)


@pytest.mark.parametrize("since_offset", EXTREME_OFFSETS)
@pytest.mark.parametrize("row_offset", EXTREME_OFFSETS)
def test_index_date_bound_preserves_opposing_extreme_iso_offsets(billing, since_offset, row_offset):
    _subject, _llm, store, _now = billing
    boundary = datetime(2026, 11, 11, tzinfo=timezone(since_offset))
    for delta, cost in ((-1, 1), (0, 2), (1, 4)):
        stamp = (boundary + timedelta(microseconds=delta)).astimezone(timezone(row_offset))
        record(store, "ask", cost, stamp.isoformat())
    record(store, "translate", 8, boundary.isoformat())
    assert store.runs.llm_cost_since(boundary.isoformat()) == 14
    assert store.runs.llm_cost_since(boundary.isoformat(), task="ask") == 6


@pytest.mark.parametrize("task", [None, "ask"])
def test_budget_query_uses_existing_created_at_index_before_exact_utc_filter(
    billing, monkeypatch, task
):
    _subject, _llm, store, _now = billing
    fetch = store._db.fetch
    statements = []

    def capture(statement):
        statements.append(statement)
        return fetch(statement)

    monkeypatch.setattr(store._db, "fetch", capture)
    assert store.runs.llm_cost_since("2026-11-11T00:00:00+00:00", task=task) == 0
    assert len(statements) == 1
    sql = str(
        statements[0].compile(
            dialect=store._db._engine.dialect, compile_kwargs={"literal_binds": True}
        )
    )
    plan = fetch(text("EXPLAIN QUERY PLAN " + sql))
    assert any("SEARCH llm_call USING INDEX ix_llm_call_created_at" in row.detail for row in plan)
