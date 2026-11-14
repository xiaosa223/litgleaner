"""配置模板里列出的真实来源都能被组装出来，并且组装阶段不连网。"""

import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest
import yaml

from litgleaner.adapters.llm.base import HttpLLM
from litgleaner.bootstrap import FULLTEXT, LLMS, SOURCES, build_context
from litgleaner.domain.errors import ConfigError
from litgleaner.infra.config import load_config

ROOT = Path(__file__).parents[2]
ENV = {
    "LITGLEANER_CONTACT_EMAIL": "me@example.org",
    "CORE_API_KEY": "core-key",
    "OPENALEX_API_KEY": "oa-key",
    "SEMANTIC_SCHOLAR_API_KEY": "s2-key",
    "NCBI_API_KEY": "ncbi-key",
    "ZOTERO_API_KEY": "zotero-key",
    "ANTHROPIC_API_KEY": "anthropic-key",
    "OPENAI_API_KEY": "openai-key",
    "GEMINI_API_KEY": "gemini-key",
    "COMPAT_API_KEY": "compat-key",
    "MINERU_API_TOKEN": "mineru-token",
}


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    zotero = tmp_path / "zotero"
    zotero.mkdir()
    sqlite3.connect(zotero / "zotero.sqlite").close()
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)

    def no_network(*_: object, **__: object) -> None:
        raise AssertionError("组装阶段不应发出网络请求")

    monkeypatch.setattr(httpx.Client, "send", no_network)
    return tmp_path / "config"


def _enable_everything(config: Path) -> None:
    path = config / "sources.yaml"
    sources = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in sources["sources"].values():
        entry["enabled"] = True
    sources["sources"]["zotero"]["options"] = {"data_dir": str(config.parent / "zotero")}
    path.write_text(yaml.safe_dump(sources, allow_unicode=True), encoding="utf-8")
    path = config / "app.yaml"
    app = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in app["fulltext"]:
        entry["enabled"] = True
        if entry["adapter"] == "zotero":
            entry["options"] = {"data_dir": str(config.parent / "zotero")}
    path.write_text(yaml.safe_dump(app, allow_unicode=True), encoding="utf-8")


def test_every_template_entry_builds(config: Path):
    _enable_everything(config)
    ctx = build_context(load_config(config))
    template_sources = set(yaml.safe_load((config / "sources.yaml").read_text())["sources"])
    assert set(ctx.sources) == template_sources
    assert all(source.capabilities for source in ctx.sources.values())
    assert [p.name for p in ctx.fulltext] == [
        "zotero",
        "arxiv",
        "europe_pmc",
        "unpaywall",
        "openalex",
        "semantic_scholar",
        "core",
        "crossref_tdm",
    ]


def test_template_covers_every_registered_real_adapter(config: Path):
    sources = yaml.safe_load((config / "sources.yaml").read_text())["sources"]
    fulltext = yaml.safe_load((config / "app.yaml").read_text())["fulltext"]
    real_sources = {name for name in SOURCES if not name.startswith("fake_")}
    real_fulltext = {name for name in FULLTEXT if not name.startswith("fake_")}
    assert {entry["adapter"] for entry in sources.values()} >= real_sources
    assert {entry["adapter"] for entry in fulltext} >= real_fulltext


def test_unpaywall_without_contact_email_is_a_config_error(config: Path, monkeypatch):
    _enable_everything(config)
    monkeypatch.delenv("LITGLEANER_CONTACT_EMAIL")
    with pytest.raises(ConfigError, match="unpaywall"):
        build_context(load_config(config))


def _edit_llm(config: Path, change) -> None:
    path = config / "llm.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def _enable_llms(data: dict) -> None:
    for entry in data["providers"].values():
        entry["enabled"] = True


def test_llm_template_covers_every_registered_protocol_and_builds(config: Path):
    _edit_llm(config, _enable_llms)
    providers = yaml.safe_load((config / "llm.yaml").read_text())["providers"]
    real = {name for name in LLMS if not name.startswith("fake_")}
    assert {entry["adapter"] for entry in providers.values()} >= real
    build_context(load_config(config))


def test_task_on_real_provider_builds_when_model_has_price(config: Path):
    def route(data: dict) -> None:
        _enable_llms(data)
        data["tasks"]["triage"] = {
            "provider": "anthropic",
            "model": "claude-haiku-5-5",
            "prompt": "triage",
        }

    _edit_llm(config, route)
    ctx = build_context(load_config(config))
    assert isinstance(ctx.llm.routes["triage"].llm, HttpLLM)


def test_task_model_without_price_builds_with_unknown_price(config: Path):
    """ADR 0013：价格未知不再是配置错误，路由照常构建，价格查询返回 None。"""

    def route(data: dict) -> None:
        _enable_llms(data)
        data["tasks"]["assess"] = {"provider": "openai", "model": "some-model", "prompt": "assess"}

    _edit_llm(config, route)
    ctx = build_context(load_config(config))
    assess = ctx.llm.routes["assess"]
    assert assess.llm.price(assess.model) is None


def test_declared_llm_key_missing_is_a_config_error(config: Path, monkeypatch):
    _edit_llm(config, _enable_llms)
    monkeypatch.delenv("GEMINI_API_KEY")
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        build_context(load_config(config))
