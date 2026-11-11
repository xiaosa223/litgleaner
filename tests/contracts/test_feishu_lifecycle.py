"""真实 SDK 和事件循环配合假端点、假 WebSocket，验证可控退出和重启。"""

import asyncio
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from litgleaner.adapters.feishu import lifecycle
from litgleaner.adapters.feishu.api import sdk
from litgleaner.adapters.feishu.listener import FeishuListener
from litgleaner.domain.errors import AuthFailed, ConfigError, SchemaChanged

ENDPOINT = {
    "code": 0,
    "data": {
        "URL": "wss://feishu.example/ws?device_id=device&service_id=1",
        "ClientConfig": {
            "ReconnectCount": -1,
            "ReconnectInterval": 1,
            "ReconnectNonce": 0,
            "PingInterval": 1,
        },
    },
}


class Socket:
    def __init__(self):
        self.closed = False
        self.sent = []
        self.fail = None
        self.transport = SimpleNamespace(abort=self.abort)

    async def recv(self):
        while self.fail is None:
            await asyncio.sleep(0.01)
        raise self.fail

    async def close(self):
        self.closed = True

    async def send(self, data):
        self.sent.append(data)

    def abort(self):
        self.closed = True


class ControlledSDK:
    def __init__(self, monkeypatch, endpoint=None, handshake=None, ping=None):
        self.module = sdk("lark_oapi.ws.client")
        self.old_loop = self.module.loop
        self.clients, self.loops, self.sockets, self.requests = [], [], [], []
        self.ready = threading.Event()
        original_client = self.module.Client
        original_http = httpx.AsyncClient

        def client(*args, **kwargs):
            value = original_client(*args, **kwargs)
            value._reconnect_nonce = 0
            value._reconnect_interval = 0.01
            if ping is not None:
                value._ping_loop = ping
            self.clients.append(value)
            self.loops.append(asyncio.get_event_loop())
            return value

        async def http(request):
            self.requests.append(request)
            if endpoint is not None:
                return await endpoint(request)
            return httpx.Response(200, json=ENDPOINT)

        async def connect(url, **kwargs):
            assert url.startswith("wss://feishu.example/")
            if handshake is not None:
                return await handshake(url)
            socket = Socket()
            self.sockets.append(socket)
            self.ready.set()
            return socket

        monkeypatch.setattr(self.module, "Client", client)
        monkeypatch.setattr(self.module.websockets, "connect", connect)
        monkeypatch.setattr(
            lifecycle.httpx,
            "AsyncClient",
            lambda **kwargs: original_http(transport=httpx.MockTransport(http), **kwargs),
        )

    def listener(self):
        return FeishuListener("app", "secret", frozenset({"ou_me"}), lambda _: None)


def run(listener):
    errors = []

    def target():
        try:
            listener.start()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=target, name="test-listener", daemon=True)
    thread.start()
    return thread, errors


def stopped(listener, thread, errors):
    listener.stop()
    thread.join(5)
    assert not thread.is_alive()
    assert errors == []


def test_stop_before_start_does_not_load_sdk(monkeypatch):
    def forbidden(*_args):
        pytest.fail("stop-before-start 不应加载 SDK")

    monkeypatch.setattr(lifecycle, "sdk", forbidden)
    listener = FeishuListener("app", "secret", frozenset(), lambda _: None)
    listener.stop()
    listener.start()


def test_two_owned_lifecycles_close_all_sdk_tasks_and_restore_other_loop(monkeypatch):
    controlled = ControlledSDK(monkeypatch)
    for _ in range(2):
        controlled.ready.clear()
        listener = controlled.listener()
        thread, errors = run(listener)
        try:
            assert controlled.ready.wait(2)
        finally:
            stopped(listener, thread, errors)
    assert all(loop.is_closed() for loop in controlled.loops)
    assert len(set(controlled.loops)) == 2
    assert all(socket.closed for socket in controlled.sockets)
    assert all(client._cache._cron.cancelled() for client in controlled.clients)
    assert controlled.module.loop is controlled.old_loop
    assert not controlled.old_loop.is_closed()
    assert len(controlled.requests) == 2


def test_second_listener_cannot_share_the_sdk_global_loop(monkeypatch):
    controlled = ControlledSDK(monkeypatch)
    listener = controlled.listener()
    thread, errors = run(listener)
    try:
        assert controlled.ready.wait(2)
        with pytest.raises(ConfigError, match="同一进程"):
            controlled.listener().start()
    finally:
        stopped(listener, thread, errors)


@pytest.mark.parametrize("phase", ["endpoint", "handshake"])
def test_stop_cancels_pending_network_work_without_waiting_for_network_timeout(monkeypatch, phase):
    waiting = threading.Event()

    async def blocked(_request):
        waiting.set()
        await asyncio.Future()

    controlled = ControlledSDK(
        monkeypatch,
        endpoint=blocked if phase == "endpoint" else None,
        handshake=blocked if phase == "handshake" else None,
    )
    listener = controlled.listener()
    thread, errors = run(listener)
    try:
        assert waiting.wait(2)
        before = time.monotonic()
    finally:
        stopped(listener, thread, errors)
    assert time.monotonic() - before < 2
    assert all(loop.is_closed() for loop in controlled.loops)


@pytest.mark.parametrize(
    "status, body, error",
    [
        (401, {}, AuthFailed),
        (403, {}, AuthFailed),
        (200, {"code": 1901, "msg": "secret must not escape"}, AuthFailed),
        (200, {"code": True}, SchemaChanged),
        (200, {"code": 0, "data": {}}, SchemaChanged),
        (200, {"code": 0, "data": {"URL": "http://unsafe.example/"}}, SchemaChanged),
        (
            200,
            {"code": 0, "data": {"URL": ENDPOINT["data"]["URL"], "ClientConfig": {}}},
            SchemaChanged,
        ),
        (200, {"code": 0, "data": {"URL": "wss://feishu.example/ws"}}, SchemaChanged),
        (302, {}, SchemaChanged),
    ],
)
def test_permanent_endpoint_errors_exit_cleanly_without_leaking_remote_messages(
    monkeypatch, status, body, error
):
    async def endpoint(_request):
        return httpx.Response(status, json=body)

    controlled = ControlledSDK(monkeypatch, endpoint=endpoint)
    thread, errors = run(controlled.listener())
    thread.join(3)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], error)
    assert "secret" not in str(errors[0])
    assert len(controlled.requests) == 1
    assert all(loop.is_closed() for loop in controlled.loops)


@pytest.mark.parametrize("status", [429, 503])
def test_transient_endpoint_failure_reconnects_then_stops(monkeypatch, status):
    calls = []

    async def endpoint(_request):
        calls.append(1)
        return (
            httpx.Response(status, json={})
            if len(calls) == 1
            else httpx.Response(200, json=ENDPOINT)
        )

    controlled = ControlledSDK(monkeypatch, endpoint=endpoint)
    listener = controlled.listener()
    thread, errors = run(listener)
    try:
        assert controlled.ready.wait(2)
    finally:
        stopped(listener, thread, errors)
    assert len(calls) == 2


def test_reconnect_authentication_failure_cleans_up_background_connection(monkeypatch):
    calls = []

    async def endpoint(_request):
        calls.append(1)
        return httpx.Response(200, json=ENDPOINT) if len(calls) == 1 else httpx.Response(403)

    controlled = ControlledSDK(monkeypatch, endpoint=endpoint)
    listener = controlled.listener()
    thread, errors = run(listener)
    try:
        assert controlled.ready.wait(2)
        controlled.sockets[0].fail = ConnectionError("lost")
        thread.join(3)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], AuthFailed)
        assert controlled.sockets[0].closed and controlled.loops[0].is_closed()
    finally:
        listener.stop()
        thread.join(3)


@pytest.mark.parametrize("phase", ["endpoint", "handshake"])
def test_network_total_timeout_allows_reconnect(monkeypatch, phase):
    calls = []

    async def endpoint(_request):
        calls.append("endpoint")
        if phase == "endpoint" and calls.count("endpoint") == 1:
            await asyncio.Future()
        return httpx.Response(200, json=ENDPOINT)

    async def handshake(_url):
        calls.append("handshake")
        if phase == "handshake" and calls.count("handshake") == 1:
            await asyncio.Future()
        socket = Socket()
        controlled.sockets.append(socket)
        controlled.ready.set()
        return socket

    monkeypatch.setattr(lifecycle, "ENDPOINT_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(lifecycle, "CONNECT_TIMEOUT_SECONDS", 0.05)
    controlled = ControlledSDK(monkeypatch, endpoint=endpoint, handshake=handshake)
    listener = controlled.listener()
    thread, errors = run(listener)
    try:
        assert controlled.ready.wait(2)
    finally:
        stopped(listener, thread, errors)
    assert calls.count(phase) == 2


def test_shutdown_aborts_transport_if_sdk_disconnect_stalls(monkeypatch):
    controlled = ControlledSDK(monkeypatch)
    monkeypatch.setattr(lifecycle, "DISCONNECT_TIMEOUT_SECONDS", 0.05)
    listener = controlled.listener()
    thread, errors = run(listener)
    try:
        assert controlled.ready.wait(2)

        async def blocked_close():
            await asyncio.Future()

        controlled.clients[0]._disconnect = blocked_close
    finally:
        stopped(listener, thread, errors)
    assert controlled.sockets[0].closed
    assert controlled.loops[0].is_closed()


def test_running_external_sdk_loop_is_left_untouched(monkeypatch):
    controlled = ControlledSDK(monkeypatch)
    external = asyncio.new_event_loop()
    started = threading.Event()

    def run_external():
        external.call_soon(started.set)
        external.run_forever()

    worker = threading.Thread(target=run_external)
    worker.start()
    try:
        assert started.wait(2)
        monkeypatch.setattr(controlled.module, "loop", external)
        with pytest.raises(ConfigError, match="占用"):
            controlled.listener().start()
        assert external.is_running() and controlled.module.loop is external
    finally:
        external.call_soon_threadsafe(external.stop)
        worker.join(2)
        external.close()


def test_listener_channel_permissions_are_an_immutable_snapshot():
    permissions = {"a": frozenset({"ou_a"})}
    listener = FeishuListener(
        "app", "secret", frozenset({"ou_a"}), lambda _: None, channels=permissions
    )
    permissions["b"] = frozenset({"ou_b"})
    assert listener.channels == {"a": frozenset({"ou_a"})}
    with pytest.raises(TypeError):
        listener.channels["b"] = frozenset({"ou_b"})


@pytest.mark.parametrize(
    "config",
    [
        {},
        [],
        False,
        "",
        [1],
        {
            **ENDPOINT["data"]["ClientConfig"],
            "PingInterval": 0,
        },
    ],
)
def test_invalid_connection_config_is_a_permanent_schema_error(monkeypatch, config):
    async def endpoint(_request):
        body = {**ENDPOINT, "data": {**ENDPOINT["data"], "ClientConfig": config}}
        return httpx.Response(200, json=body)

    controlled = ControlledSDK(monkeypatch, endpoint=endpoint)
    thread, errors = run(controlled.listener())
    thread.join(3)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], SchemaChanged)
    assert len(controlled.requests) == 1


def test_shutdown_cancels_the_sdk_ping_finally_sleep_a_second_time(monkeypatch):
    active, delayed = threading.Event(), threading.Event()
    tasks = []

    async def ping():
        tasks.append(asyncio.current_task())
        active.set()
        try:
            await asyncio.Future()
        finally:
            delayed.set()
            await asyncio.sleep(3600)

    controlled = ControlledSDK(monkeypatch, ping=ping)
    listener = controlled.listener()
    thread, errors = run(listener)
    try:
        assert active.wait(2)
    finally:
        stopped(listener, thread, errors)
    assert delayed.is_set() and tasks[0].cancelled()
    assert controlled.sockets[0].closed and controlled.loops[0].is_closed()
