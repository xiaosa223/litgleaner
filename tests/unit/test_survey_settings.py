"""调研配置边界、运行时接线及来源配置指纹不读取环境凭据。"""

from dataclasses import replace
from datetime import date
from zoneinfo import ZoneInfo

import pytest
import yaml
from pydantic import ValidationError
from tests.e2e.conftest import _write_config

from litgleaner.application.context import RuntimeSettings
from litgleaner.bootstrap import _survey_sources_version, build_context
from litgleaner.domain.chunker import ChunkerConfig
from litgleaner.domain.survey import SurveySettings
from litgleaner.infra.config import AppSettings, load_config
from litgleaner.infra.db.migrate import upgrade_database


def test_survey_defaults_preserve_old_runtime_and_app_config_constructor():
    runtime = RuntimeSettings(3, 300, 48, 6000, ChunkerConfig(), ZoneInfo("UTC"))
    app = AppSettings(
        database="db",
        blob_root="blobs",
        archive_dir="archive",
        prompts_dir="prompts",
        timezone="UTC",
    )
    assert runtime.survey == app.survey == SurveySettings()
    assert runtime.survey_sources_version == ""


@pytest.mark.parametrize(
    "field,value",
    [
        ("rounds", 0),
        ("rounds", 11),
        ("rounds", True),
        ("candidate_limit", "2"),
        ("candidate_limit", 1001),
        ("category_limit", 0),
        ("expansion_limit", 0),
        ("seed_limit", 51),
        ("coverage_threshold", -0.1),
        ("coverage_threshold", 1.1),
    ],
)
def test_settings_reject_out_of_bounds_and_coerced_limits(field, value):
    with pytest.raises(ValidationError):
        SurveySettings(**{field: value})


def test_config_schema_advertises_limits_and_bootstrap_preserves_explicit_values(tmp_path):
    _write_config(tmp_path)
    path = tmp_path / "config/app.yaml"
    body = yaml.safe_load(path.read_text())
    body["survey"] = {
        "rounds": 3,
        "candidate_limit": 20,
        "since": "2001-02-03",
        "coverage_threshold": 0.2,
    }
    path.write_text(yaml.safe_dump(body))
    config = load_config(tmp_path / "config")
    upgrade_database(config.database_url)
    context = build_context(config)
    try:
        assert context.settings.survey == config.app.survey
        assert context.settings.survey.since == date(2001, 2, 3)
        assert context.settings.survey.rounds == 3
        assert context.settings.survey_sources_version == _survey_sources_version(config)
        assert context.settings.survey_sources_version
    finally:
        context.store.dispose()
    schema = AppSettings.model_json_schema()["$defs"]["SurveySettings"]
    assert schema["properties"]["candidate_limit"]["maximum"] == 1000
    assert schema["properties"]["rounds"]["minimum"] == 1


def test_source_version_changes_for_sources_fulltext_parser_structure_not_environment(
    tmp_path, monkeypatch
):
    _write_config(tmp_path)
    config = load_config(tmp_path / "config")
    original = _survey_sources_version(config)
    monkeypatch.setenv("SOURCE_TEST_SECRET", "first-fake")
    assert _survey_sources_version(config) == original
    monkeypatch.setenv("SOURCE_TEST_SECRET", "second-fake")
    assert _survey_sources_version(config) == original
    source = config.sources["alpha"]
    for changed in (
        replace(config, sources={"alpha": source.model_copy(update={"enabled": False})}),
        replace(config, sources={"alpha": source.model_copy(update={"options": {"limit": 5}})}),
        replace(
            config,
            sources={"alpha": source.model_copy(update={"optional_env": ("SOURCE_TEST_SECRET",)})},
        ),
        replace(config, app=config.app.model_copy(update={"fulltext": ()})),
        replace(
            config,
            app=config.app.model_copy(
                update={
                    "parser": config.app.parser.model_copy(update={"options": {"version": "v2"}})
                }
            ),
        ),
    ):
        assert _survey_sources_version(changed) != original
