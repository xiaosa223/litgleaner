"""语言配置独立于模型，正文同语言跳过采用保守的中英文规则。"""

from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from litgleaner.application.context import RuntimeSettings
from litgleaner.domain.chunker import ChunkerConfig
from litgleaner.domain.language import language_name, needs_translation, source_language
from litgleaner.infra.config import AppSettings, LanguageSettings


@pytest.mark.parametrize(
    "system,expected",
    [
        ("zh_CN", "zh-CN"),
        ("zh_TW", "zh-CN"),
        ("ZH_HK", "zh-CN"),
        ("en_US", "en"),
        ("fr_FR", "en"),
        (None, "en"),
    ],
)
def test_missing_language_follows_supported_system_language(monkeypatch, system, expected):
    monkeypatch.setattr("litgleaner.infra.config.locale.getlocale", lambda: (system, "UTF-8"))
    settings = LanguageSettings()
    assert settings.ui == settings.reading == expected
    app = AppSettings(
        database="db",
        blob_root="blobs",
        archive_dir="archive",
        prompts_dir="prompts",
        timezone="UTC",
        parser={"adapter": "fake_parser"},
    )
    assert app.language.ui == app.language.reading == expected


def test_invalid_system_locale_falls_back_without_blocking_startup(monkeypatch):
    def broken():
        raise ValueError("unknown locale")

    monkeypatch.setattr("litgleaner.infra.config.locale.getlocale", broken)
    assert LanguageSettings().ui == LanguageSettings().reading == "zh-CN"


@pytest.mark.parametrize("ui", ["zh-CN", "en"])
def test_reading_language_defaults_to_explicit_ui(monkeypatch, ui):
    monkeypatch.setattr("litgleaner.infra.config.locale.getlocale", lambda: ("fr_FR", "UTF-8"))
    settings = LanguageSettings.model_validate({"ui": ui})
    assert settings.ui == settings.reading == ui


def test_ui_and_reading_language_are_independent():
    settings = LanguageSettings(ui="en", reading="zh-CN")
    assert settings.ui == "en" and settings.reading == "zh-CN"
    settings = LanguageSettings(ui="zh-CN", reading="en")
    assert settings.ui == "zh-CN" and settings.reading == "en"


def test_explicit_ui_does_not_consult_the_system_locale(monkeypatch):
    def unexpected():
        pytest.fail("显式语言配置不应读取系统 locale")

    monkeypatch.setattr("litgleaner.infra.config.locale.getlocale", unexpected)
    assert LanguageSettings(ui="en").reading == "en"


@pytest.mark.parametrize(
    "fields",
    [
        {"ui": "fr"},
        {"reading": "fr"},
        {"ui": "zh"},
        {"ui": "en-US"},
        {"reading": ""},
        {"ui": None},
        {"reading": None},
        {"unknown": "en"},
    ],
)
def test_language_configuration_rejects_unsupported_codes_and_unknown_fields(fields):
    with pytest.raises(ValidationError):
        LanguageSettings.model_validate(fields)


def test_runtime_manual_construction_remains_chinese_by_default():
    settings = RuntimeSettings(3, 300, 48, 6000, ChunkerConfig(), ZoneInfo("UTC"))
    assert settings.ui_language == settings.reading_language == "zh-CN"


def test_language_names_are_prompt_parameters():
    assert language_name("zh-CN") == "简体中文"
    assert language_name("en") == "English"


@pytest.mark.parametrize(
    "text", ["我们研究这个问题。", "卷积神经网络提高了准确率。", "方法", "结果提高了 3.5%。"]
)
def test_clear_chinese_is_skipped_only_for_chinese(text):
    assert source_language(text) == "zh-CN"
    assert not needs_translation(text, "zh-CN")
    assert needs_translation(text, "en")


@pytest.mark.parametrize(
    "text",
    [
        "We study the problem.",
        "We found it works.",
        "This is an English paragraph.",
        "The model is evaluated on the benchmark.",
        "Abstract—Study of Top Paper.",
    ],
)
def test_clear_english_is_skipped_only_for_english(text):
    assert source_language(text) == "en"
    assert not needs_translation(text, "en")
    assert needs_translation(text, "zh-CN")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "123 + 456",
        "x = y",
        "Attention Mechanism",
        "Transformer",
        "We study this problem. 我们研究这个问题。",
        "我们使用 EEG 信号。",
        "神経回路網を用いる。",
        "神經網絡の方法",
        "ニューラルネットワーク",
        "ﾆｭｰﾗﾙ",
        "Les méthodes sont différentes.",
        "Le modele est une methode.",
        "El modelo es una prueba.",
        "대한민국 연구",
        "Το μοντέλο είναι καλό.",
    ],
)
def test_mixed_or_uncertain_text_keeps_translation_available(text):
    assert source_language(text) is None
    assert needs_translation(text, "zh-CN") and needs_translation(text, "en")
