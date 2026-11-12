"""任务预算与全局预算的优先级、查询边界和 UTC 推迟时间。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import Mock, call

import pytest

from litgleaner.application.llm_gateway import LLMGateway, Prompt, TaskRoute
from litgleaner.domain.errors import (
    BudgetExceeded,
    Deferred,
    ModelConfigurationRequired,
    TaskBudgetExceeded,
    WaitingForWork,
)
from litgleaner.ports.llm import LLM, LLMResponse
from litgleaner.ports.store import RunRepo


def gateway(*, cap=None, now=None, global_budget=2):
    runs = Mock(spec=RunRepo)
    runs.llm_cost_since.return_value = 0
    llm = Mock(spec=LLM)
    llm.complete.return_value = LLMResponse({}, "response-model", 1, 1, 0.01)
    route = TaskRoute(llm, "request-model", Prompt("ask", "prompt"), daily_budget_usd=cap)

    def clock():
        return now or datetime(2026, 11, 11, 19, tzinfo=UTC)

    return LLMGateway({"ask": route}, runs, global_budget, clock), llm, runs


@pytest.mark.parametrize("operation", ["call", "stream", "check_budget"])
def test_missing_task_precedes_all_budget_queries(operation):
    original, llm, runs = gateway(cap=0, global_budget=0)
    subject = replace(original, routes={})
    with pytest.raises(ModelConfigurationRequired):
        if operation == "call":
            subject.call("ask", {}, "paper", {"type": "object"})
        elif operation == "stream":
            subject.stream("ask", {}, "paper")
        else:
            subject.check_budget("ask")
    runs.llm_cost_since.assert_not_called()
    runs.record_llm_call.assert_not_called()
    llm.complete.assert_not_called()
    llm.stream.assert_not_called()


@pytest.mark.parametrize("operation", ["call", "stream", "check_budget"])
def test_global_limit_precedes_task_limit_and_keeps_existing_error(operation):
    subject, llm, runs = gateway(cap=0, global_budget=0)
    with pytest.raises(BudgetExceeded):
        if operation == "call":
            subject.call("ask", {}, "paper", {"type": "object"})
        elif operation == "stream":
            subject.stream("ask", {}, "paper")
        else:
            subject.check_budget("ask")
    runs.llm_cost_since.assert_called_once_with("2026-11-11T00:00:00+00:00")
    llm.complete.assert_not_called()
    llm.stream.assert_not_called()


@pytest.mark.parametrize("operation", ["call", "stream", "check_budget"])
def test_zero_task_limit_pauses_before_request_and_preserves_deferred_contract(operation):
    subject, llm, runs = gateway(cap=0)
    with pytest.raises(TaskBudgetExceeded) as caught:
        if operation == "call":
            subject.call("ask", {}, "paper", {"type": "object"})
        elif operation == "stream":
            subject.stream("ask", {}, "paper")
        else:
            subject.check_budget("ask")
    error = caught.value
    assert isinstance(error, (Deferred, WaitingForWork))
    assert not isinstance(error, BudgetExceeded)
    assert error.task == "ask" and error.retry_after_seconds == 5 * 3600
    assert "0.0000" in str(error) and "ask" in str(error)
    assert runs.llm_cost_since.call_args_list == [
        call("2026-11-11T00:00:00+00:00"),
        call("2026-11-11T00:00:00+00:00", task="ask"),
    ]
    runs.record_llm_call.assert_not_called()
    llm.complete.assert_not_called()
    llm.stream.assert_not_called()


def test_absent_task_cap_retains_single_query_and_request_behavior():
    subject, llm, runs = gateway()
    result = subject.call("ask", {}, "paper", {"type": "object"})
    assert result.data == {} and result.model == "response-model"
    runs.llm_cost_since.assert_called_once_with("2026-11-11T00:00:00+00:00")
    llm.complete.assert_called_once()
    runs.record_llm_call.assert_called_once()


def test_connection_probe_style_budget_check_ignores_business_task_limit():
    subject, llm, runs = gateway(cap=0)
    subject.check_budget()
    runs.llm_cost_since.assert_called_once_with("2026-11-11T00:00:00+00:00")
    runs.record_llm_call.assert_not_called()
    llm.complete.assert_not_called()


@pytest.mark.parametrize(
    ("now", "day_start", "retry_seconds"),
    [
        (
            datetime(2026, 11, 12, 3, tzinfo=timezone(timedelta(hours=8))),
            "2026-11-11T00:00:00+00:00",
            5 * 3600,
        ),
        (
            datetime(2026, 11, 11, 0, tzinfo=UTC),
            "2026-11-11T00:00:00+00:00",
            24 * 3600,
        ),
        (
            datetime(2026, 11, 11, 23, 59, 59, 999999, tzinfo=UTC),
            "2026-11-11T00:00:00+00:00",
            1,
        ),
    ],
)
def test_deferred_deadline_uses_next_utc_midnight_and_rounds_up(now, day_start, retry_seconds):
    subject, _llm, runs = gateway(cap=0.2, now=now)
    runs.llm_cost_since.side_effect = [1, 0.2]
    with pytest.raises(TaskBudgetExceeded) as caught:
        subject.check_budget("ask")
    assert caught.value.retry_after_seconds == retry_seconds
    assert runs.llm_cost_since.call_args_list == [call(day_start), call(day_start, task="ask")]
    assert "0.2000" in str(caught.value)


def test_task_cap_is_not_part_of_prompt_or_provider_identity():
    subject, _llm, _runs = gateway(cap=0.2)
    before = subject.require_route("ask")
    after = replace(before, daily_budget_usd=0.5)
    assert after.prompt.version == before.prompt.version
    assert after.provider_version == before.provider_version
