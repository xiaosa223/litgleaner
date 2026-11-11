"""每日自动备份复用运行锁；重启、失败与无模型均不丢掉下一次执行机会。"""

import logging
import threading
from dataclasses import replace
from datetime import timedelta

import httpx
from typer.testing import CliRunner

import litgleaner.service as service_module
from litgleaner.cli import app
from litgleaner.cli_shared import try_run_lock
from litgleaner.infra.config import load_config
from litgleaner.service import Service
from tests.e2e.conftest import Harness, paper


def running(harness: Harness) -> Service:
    return Service(load_config(harness.root / "config"), harness.ctx, static_dir=None)


def daily_files(harness: Harness):
    return tuple(load_config(harness.root / "config").backup_dir.glob("*-daily*.db"))


def test_first_backup_and_restart_preserve_database_and_do_not_call_models(harness: Harness):
    harness.write_papers([paper("Saved evidence", 4)])
    assert harness.run().ok
    before = harness.ctx.store.stale_snapshot()
    calls = harness.llm_calls()
    running(harness)._backup_if_due()
    first = daily_files(harness)
    assert len(first) == 1 and first[0].stat().st_size > 0
    running(harness)._backup_if_due()
    assert daily_files(harness) == first
    assert harness.ctx.store.stale_snapshot() == before
    assert harness.llm_calls() == calls


def test_same_day_reuses_memory_but_next_local_day_runs_again(harness: Harness, monkeypatch):
    instance = running(harness)
    original = service_module.ensure_daily_snapshot
    calls = []

    def backup(*args, **kwargs):
        calls.append(kwargs["now"])
        return original(*args, **kwargs)

    monkeypatch.setattr(service_module, "ensure_daily_snapshot", backup)
    instance._backup_if_due()
    harness.clock.advance(hours=1)
    instance._backup_if_due()
    assert len(calls) == len(daily_files(harness)) == 1
    harness.clock.advance(days=1)
    instance._backup_if_due()
    assert len(calls) == len(daily_files(harness)) == 2


def test_deleted_cached_snapshot_is_recreated(harness: Harness):
    instance = running(harness)
    instance._backup_if_due()
    daily_files(harness)[0].unlink()
    instance._backup_if_due()
    assert len(daily_files(harness)) == 1


def test_another_process_holding_run_lock_defers_backup_without_losing_it(harness: Harness):
    config = load_config(harness.root / "config")
    instance = running(harness)
    with try_run_lock(config) as acquired:
        assert acquired
        instance._backup_if_due()
        assert not daily_files(harness)
    instance._backup_if_due()
    assert len(daily_files(harness)) == 1


def test_transient_failure_is_logged_and_retried_same_day(harness: Harness, monkeypatch, caplog):
    instance = running(harness)
    original = service_module.ensure_daily_snapshot
    attempts = []

    def flaky(*args, **kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("disk unavailable")
        return original(*args, **kwargs)

    monkeypatch.setattr(service_module, "ensure_daily_snapshot", flaky)
    with caplog.at_level(logging.ERROR):
        instance._backup_if_due()
    assert "每日备份" in caplog.text
    assert not daily_files(harness)
    instance._backup_if_due()
    assert len(attempts) == 2 and len(daily_files(harness)) == 1


def test_daily_backup_works_without_any_model_routes(harness: Harness):
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes={}))
    running(harness)._backup_if_due()
    assert len(daily_files(harness)) == 1 and harness.llm_calls() == 0


def test_schedule_runs_backup_even_when_pipeline_fails(harness: Harness, monkeypatch, caplog):
    instance = running(harness)

    def fail(*_args):
        raise RuntimeError("pipeline unavailable")

    monkeypatch.setattr(service_module, "run_due", fail)
    monkeypatch.setattr(instance, "_serve_on_demand", lambda _: instance._stop.set())
    with caplog.at_level(logging.ERROR):
        instance._schedule()
    assert len(daily_files(harness)) == 1 and "定时运行出错" in caplog.text


def test_backup_failure_does_not_stop_scheduled_work(harness: Harness, monkeypatch, caplog):
    instance = running(harness)
    ran = []

    def fail(*_args, **_kwargs):
        raise OSError("backup unavailable")

    monkeypatch.setattr(service_module, "ensure_daily_snapshot", fail)
    monkeypatch.setattr(service_module, "run_due", lambda *_: ran.append(True))
    monkeypatch.setattr(instance, "_serve_on_demand", lambda _: instance._stop.set())
    with caplog.at_level(logging.ERROR):
        instance._schedule()
    assert ran == [True] and "每日备份" in caplog.text


def test_service_respects_custom_backup_directory_and_retention(harness: Harness):
    harness.edit_yaml(
        "app.yaml", lambda data: data.update(backup={"dir": "custom/snapshots", "keep": 2})
    )
    instance = running(harness)
    for _ in range(3):
        instance._backup_if_due()
        harness.clock.advance(days=1)
    assert len(daily_files(harness)) == 2
    assert all(path.parent == harness.root / "custom/snapshots" for path in daily_files(harness))


def test_clock_moving_to_previous_day_checks_disk_instead_of_suppressing_backup(harness: Harness):
    instance = running(harness)
    instance._backup_if_due()
    harness.clock.now -= timedelta(days=1)
    instance._backup_if_due()
    assert len(daily_files(harness)) == 2


def test_two_service_instances_share_the_same_backup_lock(harness: Harness, monkeypatch):
    first, second = running(harness), running(harness)
    entered, release = threading.Event(), threading.Event()
    original = service_module.ensure_daily_snapshot
    calls = []

    def paused(*args, **kwargs):
        calls.append(True)
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(service_module, "ensure_daily_snapshot", paused)
    thread = threading.Thread(target=first._backup_if_due, daemon=True)
    thread.start()
    try:
        assert entered.wait(5)
        second._backup_if_due()
        assert calls == [True] and not daily_files(harness)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and len(daily_files(harness)) == 1
    second._backup_if_due()
    assert len(daily_files(harness)) == 1


def test_http_reading_remains_available_when_backup_fails(harness: Harness, monkeypatch):
    def fail(*_args, **_kwargs):
        raise OSError("backup directory unavailable")

    monkeypatch.setattr(service_module, "ensure_daily_snapshot", fail)
    monkeypatch.setattr(service_module, "run_due", lambda *_: None)
    instance = running(harness)
    instance.start()
    try:
        with httpx.Client(follow_redirects=True, timeout=10) as client:
            assert client.get(instance.launch_url).status_code == 200
            assert client.get(instance.base_url + "/api/inbox").status_code == 200
    finally:
        instance.stop()


def test_manual_backup_stays_available_while_pipeline_lock_is_held(harness: Harness):
    config = load_config(harness.root / "config")
    with try_run_lock(config) as acquired:
        assert acquired
        result = CliRunner().invoke(app, ["backup", "--config", str(harness.root / "config")])
    assert result.exit_code == 0, result.output
    assert len(tuple(config.backup_dir.glob("*.db"))) == 1
