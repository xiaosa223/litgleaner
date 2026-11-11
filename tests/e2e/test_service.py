"""常驻服务：真实端口上的领读器接口，加上和命令行共用运行锁的定时调度。"""

import fcntl
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from litgleaner.application import inbox
from litgleaner.infra.config import load_config
from litgleaner.service import Service, open_in_browser, run_due
from tests.e2e.conftest import Harness, paper


@pytest.fixture
def service(harness: Harness) -> Iterator[Service]:
    harness.write_papers([paper("Top Paper", 5)])
    harness.run()
    running = Service(
        load_config(harness.root / "config"), harness.ctx, static_dir=None, interval_seconds=3600
    )
    running.start()
    yield running
    running.stop()


def test_serves_reader_on_loopback_random_port(service: Service):
    assert service.launch_url.startswith("http://127.0.0.1:")
    with httpx.Client(follow_redirects=True, timeout=10) as client:
        assert client.get(f"{service.base_url}/api/inbox").status_code == 401
        assert client.get(service.launch_url).status_code == 200
        inbox = client.get(f"{service.base_url}/api/inbox").json()
    assert [item["title"] for item in inbox["recommended"]] == ["Top Paper"]


def test_stop_releases_the_port(harness: Harness):
    running = Service(load_config(harness.root / "config"), harness.ctx, static_dir=None)
    running.start()
    url = running.base_url
    running.stop()
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{url}/api/inbox", timeout=2)


def test_scheduled_run_skips_when_another_run_holds_the_lock(harness: Harness):
    config = load_config(harness.root / "config")
    harness.write_papers([paper("Later Paper", 5)])
    lock = config.path(config.app.database).with_suffix(".lock")
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run_due(config, harness.ctx) is None
    report = run_due(config, harness.ctx)
    assert report is not None and report.ok
    assert harness.llm_calls("triage") == 1


def test_browser_is_opened_via_private_file_not_token_in_argv(tmp_path, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr("litgleaner.service.webbrowser.open", opened.append)
    url = "http://127.0.0.1:1/launch?token=secret-token"

    monkeypatch.chdir(tmp_path)
    path = open_in_browser(url, Path("data"))

    assert path.stat().st_mode & 0o777 == 0o600
    assert "secret-token" in path.read_text(encoding="utf-8")
    assert opened == [path.as_uri()]
    assert "secret-token" not in opened[0]


def test_added_paper_is_processed_without_waiting_for_the_schedule(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("litgleaner.service.ON_DEMAND_TICK_SECONDS", 0.05)
    harness.write_papers([paper("Off Topic", 3, marker=" [[irrelevant]]")])
    running = Service(
        load_config(harness.root / "config"), harness.ctx, static_dir=None, interval_seconds=3600
    )
    running.start()
    try:
        time.sleep(0.3)  # 启动时的第一轮定时运行跑完
        item = inbox.add_identifier(harness.ctx, "10.1000/off-topic")
        assert item.paper_id
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = harness.ctx.store.papers.get_fulltext(item.paper_id)
            if state is not None and state.status == "parsed":
                break
            time.sleep(0.05)
        else:
            pytest.fail("添加的论文没有在 10 秒内处理")
    finally:
        running.stop()
