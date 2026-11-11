"""常驻服务组合假长连接、真实队列/数据库/运行锁和本地 HTTP。"""

import threading
import time
from dataclasses import replace

import httpx
import pytest
from sqlalchemy import select

import litgleaner.application.inbound as inbound_module
import litgleaner.service as service_module
from litgleaner.cli_shared import try_run_lock
from litgleaner.domain.library import decode_action
from litgleaner.infra.config import load_config
from litgleaner.infra.db.schema import feedback
from litgleaner.ports.inbound import CardClick
from litgleaner.service import Service
from tests.e2e.test_feishu_click import save_button_values, setup


class Listener:
    def __init__(self, on_click, *, fail=False):
        self.channels = {"me": frozenset({"ou_me"})}
        self.on_click, self.fail = on_click, fail
        self.started, self.stopped = threading.Event(), threading.Event()

    def start(self):
        self.started.set()
        if self.fail:
            raise RuntimeError("socket unavailable")
        self.stopped.wait()

    def stop(self):
        self.stopped.set()


def until(condition):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    pytest.fail("服务没有在期限内完成操作")


@pytest.fixture
def channel_service(harness, monkeypatch):
    action = decode_action(save_button_values(setup(harness))[0])
    assert action is not None
    click = CardClick(action, "ou_me", "event-1", "message-1")
    loaded = load_config(harness.root / "config")
    updates, created = [], []

    def update(message_id, body):
        with try_run_lock(loaded) as acquired:
            assert acquired, "消息更新必须在释放业务运行锁后执行"
        updates.append((message_id, body))

    def build(config, channel, on_click):
        assert config == loaded and channel is None
        listener = Listener(on_click)
        created.append(listener)
        return listener, update

    monkeypatch.setattr(service_module, "build_feishu_listener", build)
    monkeypatch.setattr(service_module, "ON_DEMAND_TICK_SECONDS", 0.02)
    monkeypatch.setattr(service_module, "run_due", lambda *_: None)
    running = Service(loaded, harness.ctx, static_dir=None, interval_seconds=60)
    running.start()
    until(lambda: created and created[0].started.is_set())
    try:
        yield running, created[0], updates, click, loaded
    finally:
        running.stop()


def test_service_click_saves_and_updates_outside_lock_without_model_work(channel_service, harness):
    running, listener, updates, click, _loaded = channel_service
    calls = harness.llm_calls()
    listener.on_click(click)
    until(lambda: len(updates) == 1)
    assert "已加入 Zotero" in updates[0][1]
    listener.on_click(click)
    until(lambda: len(updates) == 2)
    assert len(harness.ctx.library.saved) == 1
    assert len(harness.ctx.store._db.fetch(select(feedback))) == 1
    assert harness.llm_calls() == calls
    with httpx.Client(follow_redirects=True) as client:
        assert client.get(running.launch_url).status_code == 200
        assert client.get(f"{running.base_url}/api/inbox").status_code == 200


def test_lock_contention_keeps_click_queued_until_released(channel_service, harness):
    running, listener, updates, click, loaded = channel_service
    with try_run_lock(loaded) as acquired:
        assert acquired
        listener.on_click(click)
        time.sleep(0.1)
        assert updates == [] and harness.ctx.library.saved == []
        assert harness.ctx.store._db.fetch(select(feedback)) == []
        assert running._clicks.qsize() == 1
    until(lambda: len(updates) == 1)
    assert running._clicks.empty() and len(harness.ctx.library.saved) == 1


def test_update_failure_preserves_feedback_and_library_entry(channel_service, harness, caplog):
    running, listener, _updates, click, _loaded = channel_service

    def fail(*_args):
        raise RuntimeError("update unavailable")

    running._update_message = fail
    listener.on_click(click)
    until(lambda: "已记录的反馈保留" in caplog.text)
    assert len(harness.ctx.store._db.fetch(select(feedback))) == 1
    assert len(harness.ctx.library.saved) == 1
    assert running._clicks.empty()


def test_write_failure_requeues_before_acknowledging_business_success(harness, monkeypatch):
    action = decode_action(save_button_values(setup(harness))[0])
    assert action is not None
    click = CardClick(action, "ou_me", "event-1", "message-1")
    running = Service(load_config(harness.root / "config"), harness.ctx, static_dir=None)
    running._listener = Listener(running._clicks.put_nowait)
    running._clicks.put_nowait(click)
    original = inbound_module.record_action

    def fail(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(inbound_module, "record_action", fail)
    running._drain_clicks()
    assert running._clicks.qsize() == 1
    assert harness.ctx.store._db.fetch(select(feedback)) == []
    monkeypatch.setattr(inbound_module, "record_action", original)
    running._drain_clicks()
    assert running._clicks.empty() and len(harness.ctx.library.saved) == 1


def test_unauthorized_queued_click_never_writes(channel_service, harness):
    running, listener, updates, click, _loaded = channel_service
    listener.on_click(replace(click, actor="ou_other"))
    until(running._clicks.empty)
    assert harness.ctx.store._db.fetch(select(feedback)) == []
    assert harness.ctx.library.saved == [] and updates == []


def test_stop_closes_listener_and_joins_all_service_threads(channel_service):
    running, listener, _updates, _click, _loaded = channel_service
    threads = tuple(running._threads)
    running.stop()
    assert listener.stopped.is_set()
    assert all(not thread.is_alive() for thread in threads)


def test_listener_failure_does_not_stop_reader_or_scheduler(harness, monkeypatch, caplog):
    scheduled = threading.Event()
    listeners = []

    def build(_config, _channel, on_click):
        listener = Listener(on_click, fail=True)
        listeners.append(listener)
        return listener, lambda *_: None

    monkeypatch.setattr(service_module, "build_feishu_listener", build)
    monkeypatch.setattr(service_module, "run_due", lambda *_: scheduled.set())
    running = Service(load_config(harness.root / "config"), harness.ctx, static_dir=None)
    running.start()
    try:
        until(lambda: listeners and listeners[0].started.is_set())
        assert scheduled.wait(5)
        until(lambda: "渠道监听失败" in caplog.text)
        assert httpx.get(f"{running.base_url}/api/inbox").status_code == 401
    finally:
        running.stop()
