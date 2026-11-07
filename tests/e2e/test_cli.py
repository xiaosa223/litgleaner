"""命令行：用演示配置走一遍用户真正会执行的命令。"""

import fcntl
import json
import logging
import shutil
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from litgleaner.cli import app, configure_logging

ROOT = Path(__file__).parents[2]
runner = CliRunner()


@pytest.fixture
def config(tmp_path: Path) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    shutil.copytree(ROOT / "prompts", tmp_path / "prompts")
    return tmp_path / "config"


def invoke(config: Path, *args: str, code: int = 0) -> str:
    result = runner.invoke(app, [*args, "--config", str(config)])
    assert result.exit_code == code, result.output
    return result.output


def test_run_refuses_outdated_database_with_instruction(config: Path):
    assert "db upgrade" in invoke(config, "run", code=2)


def test_full_demo_flow(config: Path):
    invoke(config, "db", "upgrade")
    assert "没有启用中的主题" in invoke(config, "doctor")
    invoke(config, "topic", "activate", "demo-topic")
    assert "demo-topic\tactive" in invoke(config, "topic", "list")

    assert "publish: 完成" in invoke(config, "run")
    assert "假适配器" in invoke(config, "doctor")
    assert "paper\t5" in invoke(config, "stats")

    outbox = config.parent / "data" / "demo" / "outbox.jsonl"
    sent = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
    assert len(sent) == 1
    assert "分·必读" in sent[0]["body"]

    assert "待初筛：0 篇" in invoke(config, "topic", "backfill", "demo-topic")
    assert "待重评：0 篇" in invoke(config, "rubric", "reassess", "demo-topic")
    assert invoke(config, "rubric", "rescore")
    assert "已处理" in invoke(config, "archive", "rebuild")
    assert "共 0 个" in invoke(config, "jobs", "list")
    assert "已放回 0 个" in invoke(config, "jobs", "retry")
    invoke(config, "topic", "pause", "demo-topic")


def test_unknown_stage_and_topic_are_friendly(config: Path):
    invoke(config, "db", "upgrade")
    result = runner.invoke(app, ["run", "--stage", "nope", "--config", str(config)])
    assert result.exit_code == 2
    assert "未知阶段" in result.output
    result = runner.invoke(app, ["topic", "activate", "nope", "--config", str(config)])
    assert result.exit_code == 2
    assert "未找到主题" in result.output


def test_invalid_config_exits_two_with_filename(config: Path):
    path = config / "app.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["typo"] = 1
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 2
    assert "app.yaml" in result.output


def test_doctor_reports_missing_credentials_by_name_only(config: Path, monkeypatch):
    monkeypatch.delenv("SOME_SECRET", raising=False)
    path = config / "sources.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["sources"]["demo"]["credentials_env"] = ["SOME_SECRET"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    invoke(config, "db", "upgrade")
    assert "SOME_SECRET" in invoke(config, "doctor", code=1)
    monkeypatch.setenv("SOME_SECRET", "topsecretvalue")
    assert "topsecretvalue" not in invoke(config, "doctor")


def test_doctor_only_warns_about_missing_optional_credentials(config: Path, monkeypatch):
    monkeypatch.delenv("SOME_OPTIONAL_KEY", raising=False)
    path = config / "sources.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["sources"]["demo"]["optional_env"] = ["SOME_OPTIONAL_KEY"]
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    invoke(config, "db", "upgrade")
    output = invoke(config, "doctor")
    assert "SOME_OPTIONAL_KEY" in output
    assert "可选" in output


def test_doctor_asks_for_contact_email_when_real_sources_are_enabled(config: Path, monkeypatch):
    monkeypatch.delenv("LITGLEANER_CONTACT_EMAIL", raising=False)
    path = config / "sources.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["sources"]["crossref"] = {"adapter": "crossref"}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    invoke(config, "db", "upgrade")
    assert "LITGLEANER_CONTACT_EMAIL" in invoke(config, "doctor")
    monkeypatch.setenv("LITGLEANER_CONTACT_EMAIL", "me@example.org")
    assert "LITGLEANER_CONTACT_EMAIL" not in invoke(config, "doctor")


def test_doctor_reports_adapter_config_errors_instead_of_crashing(config: Path, monkeypatch):
    monkeypatch.delenv("LITGLEANER_CONTACT_EMAIL", raising=False)
    path = config / "app.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["fulltext"] = [{"adapter": "unpaywall"}]
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    invoke(config, "db", "upgrade")
    assert "LITGLEANER_CONTACT_EMAIL" in invoke(config, "doctor", code=1)


def test_second_run_exits_while_lock_is_held(config: Path):
    invoke(config, "db", "upgrade")
    lock = config.parent / "data" / "demo" / "litgleaner.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert "已有流程" in invoke(config, "run", code=3)
        assert "已有流程" in invoke(config, "rubric", "rescore", code=3)
        assert "已有流程" in invoke(config, "archive", "rebuild", code=3)


def test_delivery_commands(config: Path):
    invoke(config, "db", "upgrade")
    assert "共 0 个" in invoke(config, "delivery", "list")
    result = runner.invoke(
        app, ["delivery", "resolve", "nope", "me", "--sent", "--config", str(config)]
    )
    assert result.exit_code == 2
    assert "不存在" in result.output


def test_logging_never_records_signed_urls_from_http_client():
    configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
