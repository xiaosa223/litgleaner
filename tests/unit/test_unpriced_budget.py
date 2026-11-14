"""价格未知的模型（ADR 0013）：美元预算只算已知费用，另按每日 token 上限控制。"""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from litgleaner.application.llm_gateway import LLMGateway, Prompt, TaskRoute
from litgleaner.domain.errors import BudgetExceeded
from litgleaner.ports.llm import LLM, LLMResponse
from litgleaner.ports.store import RunRepo

NOW = datetime(2026, 11, 14, 9, tzinfo=UTC)


def _gateway(*, price, used_tokens=0, limit=1000):
    runs = Mock(spec=RunRepo)
    runs.llm_cost_since.return_value = 0
    runs.llm_unpriced_tokens_since.return_value = used_tokens
    llm = Mock(spec=LLM)
    llm.price.return_value = price
    llm.complete.return_value = LLMResponse({}, "m", 10, 5, None if price is None else 0.0)
    route = TaskRoute(llm, "m", Prompt("ask", "prompt"))
    gateway = LLMGateway({"ask": route}, runs, 2.0, lambda: NOW, daily_tokens_unpriced=limit)
    return gateway, llm, runs


def test_unpriced_call_runs_and_is_recorded_with_unknown_cost():
    gateway, _llm, runs = _gateway(price=None, used_tokens=999)
    gateway.call("ask", {}, "paper", {"type": "object"})
    recorded = runs.record_llm_call.call_args.args[0]
    assert recorded.cost_usd is None
    assert (recorded.input_tokens, recorded.output_tokens) == (10, 5)
    runs.llm_unpriced_tokens_since.assert_called_once_with("2026-11-14T00:00:00+00:00")


@pytest.mark.parametrize("used", [1000, 5000])
def test_unpriced_token_limit_stops_before_request(used):
    gateway, llm, _runs = _gateway(price=None, used_tokens=used)
    with pytest.raises(BudgetExceeded, match="token"):
        gateway.call("ask", {}, "paper", {"type": "object"})
    llm.complete.assert_not_called()


def test_zero_limit_forbids_unpriced_models():
    gateway, llm, _runs = _gateway(price=None, limit=0)
    with pytest.raises(BudgetExceeded):
        gateway.stream("ask", {}, "paper")
    llm.stream.assert_not_called()


def test_priced_route_ignores_unpriced_token_limit():
    gateway, llm, runs = _gateway(price=(0.1, 0.5), used_tokens=10**9)
    gateway.call("ask", {}, "paper", {"type": "object"})
    runs.llm_unpriced_tokens_since.assert_not_called()
    llm.complete.assert_called_once()


def test_global_check_without_task_does_not_query_tokens():
    gateway, _llm, runs = _gateway(price=None, used_tokens=10**9)
    gateway.check_budget()
    runs.llm_unpriced_tokens_since.assert_not_called()
