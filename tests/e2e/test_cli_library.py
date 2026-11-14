"""命令行：zotero add / status / authorize、feishu listen 的配置检查。不连接真实 Zotero 和飞书。"""

import shutil
from pathlib import Path

import pytest
import yaml

from tests.e2e.test_cli import ROOT, invoke


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "tests/fixtures/demo_config", tmp_path / "config")
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    return tmp_path / "config"


def enable_library(config_dir: Path, adapter: str = "fake_library") -> None:
    path = config_dir / "app.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["library"] = {"adapter": adapter, "options": {"base_url": "http://127.0.0.1:9"}}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def prepared(config_dir: Path) -> None:
    invoke(config_dir, "db", "upgrade")
    invoke(config_dir, "topic", "activate", "demo-topic")
    invoke(config_dir, "run")


def test_zotero_add_by_doi_saves_once_then_reports_existing(config_dir: Path):
    enable_library(config_dir)
    prepared(config_dir)
    assert "已加入：ITEM0" in invoke(config_dir, "zotero", "add", "https://doi.org/10.1000/demo.1")
    assert "已加入：ITEM0" in invoke(config_dir, "zotero", "add", "10.1000/DEMO.1")


def test_zotero_add_explains_missing_library_and_unknown_paper(config_dir: Path):
    prepared(config_dir)
    assert "没有启用文献库" in invoke(config_dir, "zotero", "add", "10.1000/demo.1", code=2)
    enable_library(config_dir)
    assert "库里没有这篇论文" in invoke(config_dir, "zotero", "add", "10.9/none", code=2)


def test_zotero_add_keeps_paper_queued_when_zotero_is_closed(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "local-key")
    enable_library(config_dir, "zotero_local")
    prepared(config_dir)
    output = invoke(config_dir, "zotero", "add", "10.1000/demo.1", code=1)
    assert "连不上 Zotero" in output and "留在队列里" in output


def test_zotero_add_without_authorization_says_how_to_fix(
    config_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    monkeypatch.setenv("HOME", str(tmp_path))  # 不读本机真实的 key 文件
    monkeypatch.delenv("ZOTERO_LOCAL_API_KEY", raising=False)
    enable_library(config_dir, "zotero_local")
    prepared(config_dir)
    output = invoke(config_dir, "zotero", "add", "10.1000/demo.1", code=1)
    assert "zotero authorize" in output and "jobs retry" in output


def test_zotero_status_and_authorize_report_unreachable_zotero(config_dir: Path):
    enable_library(config_dir, "zotero_local")
    invoke(config_dir, "db", "upgrade")
    assert "连不上 Zotero" in invoke(config_dir, "zotero", "status", code=2)
    assert "授权没有完成" in invoke(config_dir, "zotero", "authorize", code=2)
    assert "Zotero 暂时写不进去" in invoke(config_dir, "doctor")


def test_feishu_listen_requires_a_feishu_channel(config_dir: Path):
    invoke(config_dir, "db", "upgrade")
    assert "没有启用的飞书渠道" in invoke(config_dir, "feishu", "listen", "--channel", "me", code=2)
