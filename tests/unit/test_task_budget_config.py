"""任务限额拒绝隐式转换，配置和 HTTP 草稿保持一致。"""

import math

import pytest
from pydantic import ValidationError

from litgleaner.infra.config import LLMTask
from litgleaner.server.model_setup import ModelBody

INVALID = (True, False, "0.2", "", -1, -0.1, math.nan, math.inf, -math.inf, [], {})


def task(**changes):
    return LLMTask(provider="main", model="model", prompt="ask", **changes)


def body(**changes):
    return ModelBody.model_validate(
        {
            "provider": "personal",
            "protocol": "openai_chat",
            "model": "model",
            "tasks": ["ask", "translate"],
            "input_price": 1,
            "output_price": 3,
            **changes,
        }
    )


@pytest.mark.parametrize("value", INVALID)
def test_yaml_task_limit_rejects_non_numeric_non_finite_and_negative_values(value):
    with pytest.raises(ValidationError):
        task(daily_budget_usd=value)


@pytest.mark.parametrize("value", INVALID)
def test_http_task_limit_rejects_the_same_invalid_values(value):
    with pytest.raises(ValidationError):
        body(task_budgets_usd={"ask": value})


@pytest.mark.parametrize("value", [None, 0, 1, 0.2, 100_000_000])
def test_valid_caps_are_preserved_in_config_and_http_draft(value):
    configured = task(daily_budget_usd=value)
    draft = body(task_budgets_usd={"ask": value}).draft()
    assert configured.daily_budget_usd == value
    assert draft.task_budgets_usd == {"ask": value}


def test_omitted_cap_stays_unlimited_and_omitted_updates_remain_empty():
    assert task().daily_budget_usd is None
    assert body().draft().task_budgets_usd == {}


@pytest.mark.parametrize("budgets", [{"extract": 1}, {"unknown": 1}, None, [], "ask"])
def test_http_draft_rejects_unselected_task_names_and_non_mapping_caps(budgets):
    with pytest.raises(ValidationError):
        body(task_budgets_usd=budgets)


def test_draft_keeps_explicit_null_distinct_from_an_omitted_task():
    draft = body(task_budgets_usd={"ask": None}).draft()
    assert draft.task_budgets_usd == {"ask": None}
    assert "translate" not in draft.task_budgets_usd


def test_empty_caps_do_not_enable_any_extra_task():
    draft = body(tasks=["ask"], task_budgets_usd={}).draft()
    assert draft.tasks == ("ask",)


def test_optional_cap_is_still_part_of_the_frozen_config_model():
    configured = task(daily_budget_usd=0.2)
    with pytest.raises(ValidationError):
        configured.daily_budget_usd = 1
