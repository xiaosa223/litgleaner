"""网关把缺模型与预算状态分开，缺模型时不触及计费或适配器。"""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from litgleaner.application.llm_gateway import LLMGateway, Prompt, TaskRoute
from litgleaner.domain.errors import (
    BudgetExceeded,
    ModelConfigurationRequired,
    ModelUsage,
    SchemaChanged,
    TransientError,
)
from litgleaner.ports.llm import LLM
from litgleaner.ports.store import RunRepo


@pytest.mark.parametrize("operation", ["call", "stream", "prompt_version"])
@pytest.mark.parametrize("budget", [0.0, 1.0])
def test_missing_route_precedes_budget_and_has_stable_error(operation: str, budget: float):
    runs = Mock(spec=RunRepo)
    gateway = LLMGateway({}, runs, budget, lambda: datetime(2026, 11, 11, tzinfo=UTC))

    with pytest.raises(ModelConfigurationRequired) as caught:
        if operation == "call":
            gateway.call("extract", {}, "paper", {"type": "object"})
        elif operation == "stream":
            gateway.stream("extract", {}, "paper")
        else:
            gateway.prompt_version("extract")

    assert caught.value.code == "model_configuration_required"
    assert caught.value.task == "extract"
    assert "extract" in str(caught.value)
    runs.llm_cost_since.assert_not_called()
    runs.record_llm_call.assert_not_called()


def test_public_budget_check_reuses_the_call_budget_rule_without_recording_cost():
    runs = Mock(spec=RunRepo)
    runs.llm_cost_since.return_value = 1.0
    gateway = LLMGateway({}, runs, 1.0, lambda: datetime(2026, 11, 11, 3, tzinfo=UTC))

    with pytest.raises(BudgetExceeded):
        gateway.check_budget()

    runs.llm_cost_since.assert_called_once_with("2026-11-11T00:00:00+00:00")
    runs.record_llm_call.assert_not_called()


@pytest.mark.parametrize("known_usage", [True, False])
def test_failed_completion_records_only_reported_usage_and_reraises_the_same_error(
    known_usage: bool,
):
    runs = Mock(spec=RunRepo)
    recorded = []
    runs.record_llm_call.side_effect = lambda call, _now: recorded.append(call)
    runs.llm_cost_since.side_effect = lambda _since: sum(call.cost_usd for call in recorded)
    llm = Mock(spec=LLM)
    error = TransientError("原始解析错误")
    if known_usage:
        error.model_usage = ModelUsage("reported-model", 100, 50, 0.002)
    llm.complete.side_effect = error
    route = TaskRoute(llm, "requested-model", Prompt("triage", "prompt"))
    gateway = LLMGateway({"triage": route}, runs, 0.001, lambda: datetime(2026, 11, 11, tzinfo=UTC))

    with pytest.raises(TransientError) as caught:
        gateway.call("triage", {}, "paper", {"type": "object"})
    assert caught.value is error
    if known_usage:
        (call,) = recorded
        assert (call.model, call.input_tokens, call.output_tokens, call.cost_usd, call.ok) == (
            "reported-model",
            100,
            50,
            0.002,
            False,
        )
        assert call.prompt_version == route.prompt.version
        with pytest.raises(BudgetExceeded):
            gateway.call("triage", {}, "paper", {"type": "object"})
        llm.complete.assert_called_once()
    else:
        assert recorded == []


def test_model_usage_is_immutable_and_each_error_defaults_to_unknown():
    usage = ModelUsage("model", 100, 50, 0.002)
    with pytest.raises(FrozenInstanceError):
        usage.cost_usd = 0
    first = TransientError("first")
    first.model_usage = usage
    assert SchemaChanged("unrelated").model_usage is None
