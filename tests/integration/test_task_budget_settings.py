"""任务限额经真实 YAML 保存、重载及路由组装保留，非法草稿不触及持久化。"""

import math
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from tests.e2e.conftest import _write_config

from litgleaner.application.stages.extract import extraction_producer_version
from litgleaner.application.translation import translator_version
from litgleaner.bootstrap import build_llm
from litgleaner.domain.errors import InvalidRequest
from litgleaner.infra.config import load_config
from litgleaner.infra.model_settings import ModelConfigFile, prepare, settings_view
from litgleaner.ports.model_settings import ModelDraft
from litgleaner.ports.store import Store
from litgleaner.server.model_setup import ModelBody

PROTOCOLS = ("openai_chat",)
CAPS = {"triage": 0.1, "extract": 0.2, "assess": 0.3, "translate": 0.4}


class MemorySecrets:
    def __init__(self):
        self.calls = []

    def get(self, name):
        self.calls.append(("get", name))

    def set(self, name, value):
        self.calls.append(("set", name))

    def delete(self, name):
        self.calls.append(("delete", name))


def draft(**changes):
    return ModelDraft(
        **{
            "provider": "personal",
            "protocol": "openai_chat",
            "model": "configured-model",
            "tasks": ("triage", "translate"),
            "input_price": 1,
            "output_price": 3,
            "api_key": None,
            **changes,
        }
    )


@pytest.fixture
def file(tmp_path):
    _write_config(tmp_path)
    path = tmp_path / "config" / "llm.yaml"
    raw = yaml.safe_load(path.read_text())
    for name, cap in CAPS.items():
        raw["tasks"][name]["daily_budget_usd"] = cap
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return ModelConfigFile(load_config(path.parent), MemorySecrets())


def test_omitted_cap_updates_preserve_selected_and_unselected_limits_after_reload(file):
    previous = file.path.read_bytes()
    original = file.config.llm.tasks
    candidate, credential = prepare(file.config, draft(), PROTOCOLS)
    file.commit(candidate, credential, None)
    loaded = load_config(file.path.parent)
    assert {name: task.daily_budget_usd for name, task in loaded.llm.tasks.items()} == CAPS
    assert file.path.with_suffix(".yaml.bak").read_bytes() == previous
    assert loaded.llm.tasks["triage"].provider == "personal"
    assert loaded.llm.tasks["extract"] == original["extract"]
    assert loaded.llm.tasks["assess"] == original["assess"]
    assert file.secrets.calls == []


def test_explicit_null_clears_only_selected_limit_and_zero_is_persisted(file):
    candidate, credential = prepare(
        file.config,
        draft(task_budgets_usd={"triage": None, "translate": 0}),
        PROTOCOLS,
    )
    file.commit(candidate, credential, None)
    loaded = load_config(file.path.parent)
    assert loaded.llm.tasks["triage"].daily_budget_usd is None
    assert loaded.llm.tasks["translate"].daily_budget_usd == 0
    assert loaded.llm.tasks["extract"].daily_budget_usd == CAPS["extract"]
    assert file.config.llm.budget.daily_usd == 2


def test_new_selected_task_can_have_a_limit_without_enabling_unselected_tasks(file):
    candidate, credential = prepare(
        file.config, draft(tasks=("ask",), task_budgets_usd={"ask": 0.5}), PROTOCOLS
    )
    file.commit(candidate, credential, None)
    loaded = load_config(file.path.parent)
    assert set(loaded.llm.tasks) == set(CAPS) | {"ask"}
    assert loaded.llm.tasks["ask"].daily_budget_usd == 0.5
    assert loaded.llm.tasks["ask"].prompt == "ask"
    assert {name: loaded.llm.tasks[name].daily_budget_usd for name in CAPS} == CAPS
    assert loaded.llm.providers["personal"].options["max_output_tokens"] == 4000
    assert loaded.llm.providers["personal"].options["structured"] == "native"


@pytest.mark.parametrize("value", [True, False, "0.2", -1, math.nan, math.inf, [], {}])
def test_direct_dataclass_draft_cannot_bypass_limit_validation_or_write_secrets(file, value):
    previous = file.path.read_bytes()
    original = file.config
    with pytest.raises(InvalidRequest, match=r"^model_task_budget_invalid$"):
        prepare(
            file.config,
            draft(api_key="not-written", task_budgets_usd={"triage": value}),
            PROTOCOLS,
        )
    assert file.path.read_bytes() == previous
    assert file.config is original
    assert file.secrets.calls == []
    assert not file.path.with_suffix(".yaml.bak").exists()


@pytest.mark.parametrize("budgets", [{"extract": 1}, {"unknown": 1}, None, [], "ask"])
def test_direct_draft_rejects_unselected_keys_and_non_mapping_updates(file, budgets):
    with pytest.raises(InvalidRequest, match=r"^model_task_budget_invalid$"):
        prepare(file.config, draft(task_budgets_usd=budgets), PROTOCOLS)
    assert file.secrets.calls == []


def test_settings_status_includes_caps_and_distinguishes_unconfigured_tasks(file):
    view = settings_view(file.config, file.secrets, PROTOCOLS, set(CAPS))
    by_task = {task.task: task for task in view.tasks}
    assert {name: by_task[name].daily_budget_usd for name in CAPS} == CAPS
    assert by_task["ask"].daily_budget_usd is None
    assert by_task["ask"].provider is None
    assert view.daily_budget_usd == 10


def test_bootstrap_loads_caps_without_changing_producer_or_calling_models(file):
    config = file.config
    store = Mock(spec=Store)
    original = build_llm(config, store, lambda: datetime(2026, 11, 12, tzinfo=UTC))
    raw = yaml.safe_load(file.path.read_text())
    for task in raw["tasks"].values():
        task["daily_budget_usd"] = 0.05
    file.path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    updated = build_llm(
        load_config(file.path.parent), store, lambda: datetime(2026, 11, 12, tzinfo=UTC)
    )
    for name, old_route in original.routes.items():
        new_route = updated.routes[name]
        assert old_route.daily_budget_usd == CAPS[name]
        assert new_route.daily_budget_usd == 0.05
        assert old_route.provider_version == new_route.provider_version
        assert old_route.prompt == new_route.prompt
        assert old_route.model == new_route.model
        assert not old_route.llm.calls and not new_route.llm.calls
        assert not old_route.llm.text_calls and not new_route.llm.text_calls
    assert not store.mock_calls


def _api_price_config(file, explicit_defaults, integer_prices, trailing_slash):
    raw = yaml.safe_load(file.path.read_text())
    options = {
        "base_url": "https://models.example.test/v1" + ("/" if trailing_slash else ""),
        "prices": {"configured-model": [1, 3] if integer_prices else [1.0, 3.0]},
    }
    if explicit_defaults:
        options.update(max_output_tokens=4000, structured="native")
    raw["providers"]["personal"] = {
        "adapter": "openai_chat",
        "options": options,
    }
    for name in ("triage", "translate", "extract"):
        raw["tasks"][name].update(provider="personal", model="configured-model")
    file.path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return ModelConfigFile(load_config(file.path.parent), file.secrets)


def _cap_body(**changes):
    return ModelBody.model_validate(
        {
            "provider": "personal",
            "protocol": "openai_chat",
            "model": "configured-model",
            "tasks": ["triage", "translate", "extract"],
            "input_price": 1.0,
            "output_price": 3.0,
            "base_url": "https://models.example.test/v1",
            "daily_budget_usd": 10.0,
            "task_budgets_usd": {"translate": 0.05},
            **changes,
        }
    )


@pytest.mark.parametrize("explicit_defaults", [True, False])
@pytest.mark.parametrize("integer_prices", [True, False])
@pytest.mark.parametrize("trailing_slash", [True, False])
def test_full_api_cap_save_preserves_price_encoding_defaults_and_cache_identity(
    file, monkeypatch, explicit_defaults, integer_prices, trailing_slash
):
    file = _api_price_config(file, explicit_defaults, integer_prices, trailing_slash)
    store = Mock(spec=Store)
    clock = Mock(return_value=datetime(2026, 11, 12, tzinfo=UTC))
    settings = SimpleNamespace(reading_language="zh-CN")
    network = Mock(side_effect=AssertionError("禁止真实模型请求"))
    monkeypatch.setattr("httpx.Client.send", network)
    original = build_llm(file.config, store, clock)
    body = _cap_body()
    candidate, credential = prepare(file.config, body.draft(), PROTOCOLS)
    file.commit(candidate, credential, None)
    loaded = load_config(file.path.parent)
    updated = build_llm(loaded, store, clock)
    try:
        prices = loaded.llm.providers["personal"].options["prices"]["configured-model"]
        assert prices == [1, 3]
        assert all(type(price) is (int if integer_prices else float) for price in prices)
        options = loaded.llm.providers["personal"].options
        assert ("max_output_tokens" in options) is explicit_defaults
        assert ("structured" in options) is explicit_defaults
        assert options["base_url"].endswith("/") is trailing_slash
        assert updated.routes["translate"].daily_budget_usd == 0.05
        for name in ("triage", "translate", "extract"):
            assert original.routes[name].provider_version == updated.routes[name].provider_version
        before = SimpleNamespace(llm=original, settings=settings)
        after = SimpleNamespace(llm=updated, settings=settings)
        assert translator_version(before) == translator_version(after)
        assert extraction_producer_version(before) == extraction_producer_version(after)
        network.assert_not_called()
        assert not store.mock_calls and not file.secrets.calls
    finally:
        for gateway in (original, updated):
            for route in gateway.routes.values():
                route.llm.close()


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"input_price": 2.0}, {"prices": {"configured-model": [2.0, 3.0]}}),
        ({"max_output_tokens": 8000}, {"max_output_tokens": 8000}),
        ({"structured": "prompt"}, {"structured": "prompt"}),
        (
            {"base_url": "https://other.example.test/v1"},
            {"base_url": "https://other.example.test/v1"},
        ),
    ],
)
def test_full_api_save_keeps_real_provider_changes(file, changes, expected):
    file = _api_price_config(file, False, True, True)
    original = file.config.llm.providers["personal"]
    candidate, credential = prepare(file.config, _cap_body(**changes).draft(), PROTOCOLS)
    file.commit(candidate, credential, None)
    updated = load_config(file.path.parent).llm.providers["personal"]
    for name, value in expected.items():
        assert updated.options[name] == value
    assert updated.options != original.options
    assert original.options["prices"] == {"configured-model": [1, 3]}
    assert "max_output_tokens" not in original.options
    assert "structured" not in original.options
