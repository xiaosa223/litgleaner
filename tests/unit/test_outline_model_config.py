"""综述骨架模型独立可选，模型设置可以一次选择全部八个任务。"""

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from litgleaner.bootstrap import build_llm
from litgleaner.infra.config import AppSettings, LLMFile, load_config
from litgleaner.infra.model_settings import TASKS, prepare, settings_view
from litgleaner.ports.model_settings import ModelDraft
from litgleaner.server.model_setup import ModelBody

ROOT = Path(__file__).parents[2]


class NoSecrets:
    def get(self, _name):
        pytest.fail("没有配置凭据时不应读密钥")


def _app(**changes) -> AppSettings:
    return AppSettings(
        database="db", blob_root="blobs", archive_dir="archive", prompts_dir="prompts",
        timezone="UTC", **changes,
    )  # fmt: skip


def test_outline_task_can_be_configured_independently_and_remains_optional():
    settings = LLMFile.model_validate(
        {"tasks": {"outline": {"provider": "personal", "model": "chosen", "prompt": "outline"}}}
    )
    assert tuple(settings.tasks) == ("outline",)
    assert settings.tasks["outline"].model == "chosen"
    assert LLMFile().tasks == {}


def test_old_configuration_gets_default_outline_budget():
    assert _app().outline.token_budget == 12000


@pytest.mark.parametrize("value", [512, 12000, 64000])
def test_outline_budget_accepts_integer_bounds(value):
    assert _app(outline={"token_budget": value}).outline.token_budget == value


@pytest.mark.parametrize("value", [0, 511, 64001, True, False, "12000", 12000.0, None])
def test_outline_budget_rejects_invalid_or_coerced_values(value):
    with pytest.raises(ValidationError):
        _app(outline={"token_budget": value})


def test_model_settings_accept_outline_alone_or_all_eight_tasks():
    for tasks in (["outline"], list(TASKS)):
        body = ModelBody(
            provider="personal", protocol="openai_chat", model="chosen", tasks=tasks,
            input_price=0, output_price=0,
        )  # fmt: skip
        assert body.draft().tasks == tuple(tasks)
    assert len(TASKS) == 8 and TASKS[-2:] == ("outline", "relate")


def test_model_settings_still_reject_unknown_tasks_and_overlong_task_lists():
    for tasks in (["unimplemented"], [*TASKS, "outline"]):
        with pytest.raises(ValidationError):
            ModelBody(
                provider="personal", protocol="openai_chat", model="chosen", tasks=tasks,
                input_price=0, output_price=0,
            )  # fmt: skip


def test_outline_preparation_sets_only_selected_route_and_settings_show_readiness():
    config = load_config(ROOT / "config")
    old_tasks = {name: task for name, task in config.llm.tasks.items() if name != "outline"}
    config = replace(
        config, llm=config.llm.model_copy(update={"tasks": old_tasks, "providers": {}})
    )
    protocols = ("openai_chat",)
    before = settings_view(config, NoSecrets(), protocols, set())
    missing = next(task for task in before.tasks if task.task == "outline")
    assert missing.provider is None and missing.model is None and not missing.ready
    candidate, credential = prepare(
        config,
        ModelDraft(
            "personal",
            "openai_chat",
            "chosen",
            ("outline",),
            0,
            0,
            base_url="http://127.0.0.1:12345/v1",
        ),
        protocols,
    )
    assert credential is None and dict(config.llm.tasks) == old_tasks
    assert candidate.llm.tasks["outline"].prompt == "outline"
    assert all(candidate.llm.tasks[name] == task for name, task in old_tasks.items())
    view = settings_view(candidate, NoSecrets(), protocols, {"outline"})
    assert {task.task for task in view.tasks if task.ready} == {"outline"}
    assert len(view.tasks) == 8


def test_old_llm_configuration_builds_without_outline_or_its_prompt(tmp_path):
    config = load_config(ROOT / "config")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "triage.md").write_text("existing prompt", encoding="utf-8")
    llm = LLMFile.model_validate(
        {
            "providers": {"demo": {"adapter": "fake_llm"}},
            "tasks": {"triage": {"provider": "demo", "model": "old-model", "prompt": "triage"}},
        }
    )
    config = replace(config, root=tmp_path, llm=llm)
    gateway = build_llm(config, SimpleNamespace(runs=None), lambda: datetime.now(UTC))
    assert tuple(gateway.routes) == ("triage",)
    assert not (tmp_path / "prompts" / "outline.md").exists()
