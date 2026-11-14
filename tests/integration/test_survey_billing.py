"""临时 survey gateway 的成功、失败与流式用量归属真实账本，日预算仍共享。"""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from litgleaner.application.llm_gateway import LLMGateway, LLMOutputInvalid, Prompt, TaskRoute
from litgleaner.domain.errors import BudgetExceeded, ModelUsage, TransientError
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import llm_call
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.llm import LLM, LLMCall, LLMResponse, StreamDone, TextDelta


@pytest.fixture
def gateway(tmp_path):
    url = f"sqlite:///{tmp_path / 'billing.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    llm = Mock(spec=LLM)
    llm.complete.return_value = LLMResponse({}, "model", 10, 5, 0.2)
    route = TaskRoute(llm, "model", Prompt("triage", "prompt"))
    original = LLMGateway(
        {"triage": route}, store.runs, 10, lambda: datetime(2026, 11, 11, tzinfo=UTC)
    )
    yield original, llm, store
    store.dispose()


def test_scoped_gateway_does_not_mutate_daily_gateway_and_paper_scope_are_independent(gateway):
    original, _llm, store = gateway
    scoped = replace(original, scope_id="run:1")
    scoped.call("triage", {}, "paper", {"type": "object"}, paper_id="P")
    original.call("triage", {}, "paper", {"type": "object"}, paper_id="P")
    replace(original, scope_id="run:2").call("triage", {}, "paper", {"type": "object"})
    assert original.scope_id is None
    assert store.runs.llm_cost_for_scope("run:1") == 0.2
    assert store.runs.llm_cost_for_scope("run:2") == 0.2
    assert store.runs.llm_cost_for_scope("missing") == 0
    assert store.runs.llm_cost_for_paper("P") == 0.4
    assert store.runs.llm_cost_since("2026-11-11") == pytest.approx(0.6)
    rows = store._db.fetch(select(llm_call).order_by(llm_call.c.id))
    assert [row.scope_id for row in rows] == ["run:1", None, "run:2"]


@pytest.mark.parametrize("failure", ["schema", "known-usage", "unknown-usage"])
def test_failed_completion_attributes_reported_cost_to_scope_without_fabricating_usage(
    gateway, failure
):
    original, llm, store = gateway
    scoped = replace(original, scope_id="run:1")
    schema = {"type": "object", "required": ["required"]}
    if failure != "schema":
        error = TransientError("failure")
        if failure == "known-usage":
            error.model_usage = ModelUsage("model", 10, 5, 0.3)
        llm.complete.side_effect = error
    with pytest.raises((LLMOutputInvalid, TransientError)):
        scoped.call("triage", {}, "paper", schema)
    expected = {"schema": 0.2, "known-usage": 0.3, "unknown-usage": 0}[failure]
    assert store.runs.llm_cost_for_scope("run:1") == expected
    assert store.runs.llm_call_count() == (0 if failure == "unknown-usage" else 1)
    assert all(row.ok is False for row in store._db.fetch(select(llm_call)))


@pytest.mark.parametrize("failure", [False, True])
def test_stream_final_usage_uses_same_scope_record_path_even_on_failure(gateway, failure):
    original, llm, store = gateway
    error = TransientError("stream failure") if failure else None

    def events(_request):
        yield TextDelta("partial")
        yield StreamDone("model", 10, 5, 0.4, error=error)
        if error:
            raise error

    llm.stream.side_effect = events
    stream = replace(original, scope_id="run:stream").stream("triage", {}, "paper", paper_id="P")
    if failure:
        with pytest.raises(TransientError):
            list(stream)
    else:
        assert list(stream) == ["partial"]
    assert store.runs.llm_cost_for_scope("run:stream") == 0.4
    assert store.runs.llm_cost_for_paper("P") == 0.4
    assert store._db.fetch(select(llm_call))[0].ok is not failure


def test_scope_does_not_bypass_daily_budget_or_write_for_rejected_call(gateway):
    original, llm, store = gateway
    store.runs.record_llm_call(
        LLMCall("triage", "m", "v", 1, 1, 0.3, False, scope_id="other"), "2026-11-11T00:00:00+00:00"
    )
    scoped = replace(original, scope_id="run:1", daily_budget_usd=0.3)
    for operation in ("call", "stream"):
        with pytest.raises(BudgetExceeded):
            if operation == "call":
                scoped.call("triage", {}, "paper", {"type": "object"})
            else:
                scoped.stream("triage", {}, "paper")
    assert store.runs.llm_cost_for_scope("run:1") == 0
    llm.complete.assert_not_called()
    llm.stream.assert_not_called()
