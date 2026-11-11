"""课题地图模型是独立可选任务；旧配置继续可用。"""

import pytest
from pydantic import ValidationError

from litgleaner.infra.config import AppSettings, LLMFile
from litgleaner.server.model_setup import ModelBody


def test_map_task_can_be_configured_without_enabling_other_tasks():
    settings = LLMFile.model_validate(
        {"tasks": {"map": {"provider": "personal", "model": "chosen", "prompt": "map"}}}
    )
    assert tuple(settings.tasks) == ("map",)
    assert settings.tasks["map"].model == "chosen"
    assert LLMFile().tasks == {}


def test_existing_app_config_gets_default_map_budget():
    app = AppSettings(
        database="db",
        blob_root="blobs",
        archive_dir="archive",
        prompts_dir="prompts",
        timezone="UTC",
    )
    assert app.map.token_budget == 12000


@pytest.mark.parametrize("value", [0, 511, 64001, True, "12000"])
def test_map_budget_rejects_invalid_or_coerced_values(value):
    with pytest.raises(ValidationError):
        AppSettings(
            database="db",
            blob_root="blobs",
            archive_dir="archive",
            prompts_dir="prompts",
            timezone="UTC",
            map={"token_budget": value},
        )


def test_model_settings_accept_all_six_available_tasks():
    body = ModelBody(
        provider="personal",
        protocol="openai_chat",
        model="chosen",
        tasks=["triage", "extract", "assess", "translate", "ask", "map"],
        input_price=0,
        output_price=0,
    )
    assert body.draft().tasks[-1] == "map"
