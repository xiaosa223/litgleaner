"""第八模型任务和核实预算配置保持现有生产者与费用上限合同。"""

import pytest
from pydantic import ValidationError
from tests.integration.test_model_settings import PROTOCOLS, draft
from tests.integration.test_model_settings import config_file as mock_config_file

from litgleaner.domain.relate_settings import RelateSettings
from litgleaner.infra.config import MemoryConfig, load_config
from litgleaner.infra.model_settings import TASKS, prepare

config_file = mock_config_file


def test_relate_memory_settings_are_optional_and_strict():
    default = MemoryConfig().relate
    assert (default.token_budget, default.max_fragments_per_side, default.fragment_chars) == (
        8000,
        4,
        1600,
    )
    for invalid in ({"token_budget": True}, {"max_fragments_per_side": 0}, {"fragment_chars": 10}):
        with pytest.raises(ValidationError):
            RelateSettings(**invalid)


def test_new_relate_task_gets_default_daily_cap_without_changing_other_tasks(config_file):
    original = config_file.config
    updated, _ = prepare(original, draft(tasks=("relate",)), PROTOCOLS)
    assert updated.llm.tasks["relate"].daily_budget_usd == 0.2
    assert updated.llm.tasks["relate"].prompt == "relate"
    assert set(TASKS) == {
        "triage",
        "extract",
        "assess",
        "translate",
        "ask",
        "map",
        "outline",
        "relate",
        "guide",
        "evaluate",
        "draft",
    }
    assert all(updated.llm.tasks[key] == value for key, value in original.llm.tasks.items())


def test_explicit_none_removes_relate_cap_and_roundtrips(config_file):
    updated, name = prepare(
        config_file.config, draft(tasks=("relate",), task_budgets_usd={"relate": None}), PROTOCOLS
    )
    config_file.commit(updated, name, "test-key")
    loaded = load_config(config_file.path.parent)
    assert loaded.llm.tasks["relate"].daily_budget_usd is None
    again, _ = prepare(loaded, draft(tasks=("relate",), api_key=None), PROTOCOLS)
    assert again.llm.tasks["relate"].daily_budget_usd is None


def test_existing_relate_cap_is_preserved_on_model_settings_edit(config_file):
    updated, _ = prepare(
        config_file.config, draft(tasks=("relate",), task_budgets_usd={"relate": 0.05}), PROTOCOLS
    )
    again, _ = prepare(updated, draft(tasks=("relate",)), PROTOCOLS)
    assert again.llm.tasks["relate"].daily_budget_usd == 0.05
