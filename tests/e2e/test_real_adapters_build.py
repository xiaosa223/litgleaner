"""配置模板里列出的真实来源都能被组装出来，并且组装阶段不连网。"""

import shutil
import sqlite3
from pathlib import Path

import httpx
import pytest
import yaml

from litfactory.bootstrap import FULLTEXT, SOURCES, build_context
from litfactory.domain.errors import ConfigError
from litfactory.infra.config import load_config

ROOT = Path(__file__).parents[2]
ENV = {
    "LITFACTORY_CONTACT_EMAIL": "me@example.org",
    "CORE_API_KEY": "core-key",
    "OPENALEX_API_KEY": "oa-key",
    "SEMANTIC_SCHOLAR_API_KEY": "s2-key",
    "NCBI_API_KEY": "ncbi-key",
    "ZOTERO_API_KEY": "zotero-key",
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
    assert [p.name for p in ctx.fulltext][1:] == [
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
    monkeypatch.delenv("LITFACTORY_CONTACT_EMAIL")
    with pytest.raises(ConfigError, match="unpaywall"):
        build_context(load_config(config))
