"""账本里价格未知的调用（ADR 0013）：费用存 NULL，不算进美元合计，按 token 单独合计。"""

import pytest
from sqlalchemy import select

from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import llm_call
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.llm import LLMCall


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'unpriced.db'}"
    upgrade_database(url)
    subject = SqlStore(url)
    yield subject
    subject.dispose()


def test_unknown_cost_is_null_and_only_counts_tokens(store):
    when = "2026-11-14T08:00:00+00:00"
    store.runs.record_llm_call(LLMCall("ask", "m", "v", 100, 50, None, True), when)
    store.runs.record_llm_call(LLMCall("ask", "m", "v", 7, 3, 0.25, True), when)
    store.runs.record_llm_call(
        LLMCall("ask", "m", "v", 1000, 1000, None, True), "2026-11-13T23:00:00+00:00"
    )

    costs = [row.cost_usd for row in store._db.fetch(select(llm_call.c.cost_usd))]
    assert costs.count(None) == 2
    assert store.runs.llm_cost_since("2026-11-14T00:00:00+00:00") == 0.25
    assert store.runs.llm_unpriced_tokens_since("2026-11-14T00:00:00+00:00") == 150
    assert store.runs.average_llm_cost("ask") == 0.25
