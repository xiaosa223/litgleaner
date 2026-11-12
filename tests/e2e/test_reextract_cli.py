"""真实命令、配置、SQLite 和共享锁验证历史重提取的显式授权边界。"""

import json

import pytest
from typer.testing import CliRunner

from litgleaner import cli_library
from litgleaner.cli import app
from litgleaner.cli_shared import try_run_lock
from litgleaner.infra.config import load_config
from litgleaner.infra.db.database import Database
from tests.e2e.conftest import Harness, paper


@pytest.fixture
def history(harness: Harness) -> Harness:
    harness.write_papers([paper("First alpha", 5), paper("Second alpha", 4)])
    assert harness.run().ok
    prompt = harness.root / "prompts" / "extract.md"
    prompt.write_text(prompt.read_text() + "\nExplain the original evidence carefully.\n")
    return harness


def command(harness: Harness, *args: str):
    return CliRunner().invoke(app, ["reextract", "--config", str(harness.root / "config"), *args])


def test_real_cli_preview_is_json_and_never_writes_or_generates(history, monkeypatch):
    before = history.llm_calls()

    def forbidden(*_args, **_kwargs):
        pytest.fail("preview attempted a database/blob write or model/source call")

    monkeypatch.setattr(Database, "write", forbidden)
    monkeypatch.setattr(type(history.llm), "complete", forbidden)
    monkeypatch.setattr(type(history.ctx.blobs), "put", forbidden)
    for source in history.ctx.sources.values():
        monkeypatch.setattr(type(source), "fetch", forbidden)
    response = command(history)
    assert response.exit_code == 0, response.output
    preview = json.loads(response.stdout)
    assert preview["plan_hash"] and len(preview["items"]) == 2
    assert all(item["state"] == "ready" for item in preview["items"])
    assert history.llm_calls() == before


@pytest.mark.parametrize(
    "args",
    [
        ["--execute"],
        ["--plan-hash", "hash"],
        ["--job", "key", "--paper", "paper"],
        ["--job", "key", "--execute", "--plan-hash", "hash"],
    ],
)
def test_invalid_authorization_flags_fail_before_loading_config(monkeypatch, args):
    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid arguments reached configuration or queue work")

    monkeypatch.setattr(cli_library, "context_or_exit", forbidden)
    response = CliRunner().invoke(app, ["reextract", *args])
    assert response.exit_code == 2 and "Traceback" not in response.output


def test_shared_lock_blocks_confirmation_but_allows_preview(history):
    plan = json.loads(command(history).stdout)
    args = ("--execute", "--plan-hash", plan["plan_hash"])
    with try_run_lock(load_config(history.root / "config")) as acquired:
        assert acquired
        preview = command(history)
        blocked = command(history, *args)
    assert preview.exit_code == 0, preview.output
    assert blocked.exit_code == 3 and "已有流程" in blocked.output
    assert not history.ctx.store.reextract_snapshot().jobs


def test_repeat_paper_scope_and_bad_plan_are_handled_without_queue_writes(history):
    paper_id = history.ctx.store.reextract_snapshot().papers[0].id
    args = ("--paper", paper_id, "--paper", paper_id)
    response = command(history, *args)
    assert response.exit_code == 0, response.output
    plan = json.loads(response.stdout)
    assert [item["paper_id"] for item in plan["items"]] == [paper_id]
    invalid = command(history, *args, "--execute", "--plan-hash", "wrong")
    assert invalid.exit_code == 2 and "预览" in invalid.output
    assert "Traceback" not in invalid.output
    assert not history.ctx.store.reextract_snapshot().jobs


def test_real_cli_confirmation_consumption_status_and_retries_do_not_recharge(history):
    plan = json.loads(command(history).stdout)
    before = history.llm_calls("extract")
    args = ("--execute", "--plan-hash", plan["plan_hash"])
    first = command(history, *args)
    repeated = command(history, *args)
    assert first.exit_code == repeated.exit_code == 0, (first.output, repeated.output)
    queued = json.loads(first.stdout)
    assert [item["job_key"] for item in queued] == [
        item["job_key"] for item in json.loads(repeated.stdout)
    ]
    assert len(queued) == 2 and history.llm_calls("extract") == before
    run = ["run", "--stage", "reextract", "--config", str(history.root / "config")]
    runner = CliRunner()
    completed = runner.invoke(app, run)
    assert completed.exit_code == 0, completed.output
    assert history.llm_calls("extract") == before + 2
    for item in queued:
        response = command(history, "--job", item["job_key"])
        assert response.exit_code == 0, response.output
        status = json.loads(response.stdout)
        assert status["status"] == "done" and status["result_extraction_id"] is not None
        assert status["cost_usd"] > 0
    assert runner.invoke(app, run).exit_code == 0
    assert history.llm_calls("extract") == before + 2


def test_missing_model_file_keeps_preview_available_but_cannot_authorize(history):
    (history.root / "config" / "llm.yaml").unlink()
    response = command(history)
    assert response.exit_code == 0, response.output
    preview = json.loads(response.stdout)
    assert len(preview["items"]) == 2 and preview["estimated_usd"] is None
    assert all(item["state"] == "blocked" for item in preview["items"])
    confirm = command(history, "--execute", "--plan-hash", preview["plan_hash"])
    assert confirm.exit_code == 2 and "extract" in confirm.output
    assert not history.ctx.store.reextract_snapshot().jobs


def test_unknown_job_is_reported_without_traceback(history):
    response = command(history, "--job", "reextract:missing")
    assert response.exit_code == 2 and "不存在" in response.output
    assert "Traceback" not in response.output
