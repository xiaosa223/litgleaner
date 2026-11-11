"""下载响应完成、传输失败或取消时，临时 ZIP 都应立即关闭。"""

import asyncio
from io import BytesIO

import pytest
from fastapi import FastAPI
from starlette.requests import ClientDisconnect

from litgleaner.server import exports

_CONTENT = b"zip bytes" * 17000


@pytest.fixture
def download(monkeypatch):
    archive = BytesIO(_CONTENT)
    monkeypatch.setattr(exports, "zip_archive", lambda _entries: archive)
    app = FastAPI()
    exports.install_export_routes(app)
    route = next(
        route for route in app.routes if getattr(route, "path", None) == "/api/exports/mine"
    )
    response = route.endpoint(object())
    yield response, archive
    archive.close()


async def _receive():
    return await asyncio.Future()


def _scope(version):
    return {"type": "http", "asgi": {"version": "3.0", "spec_version": version}}


def _leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return tuple(leaf for child in error.exceptions for leaf in _leaves(child))
    return (error,)


@pytest.mark.parametrize("stage", ["http.response.start", "http.response.body"])
def test_zip_closes_immediately_when_asgi_24_transport_disconnects(download, stage):
    response, archive = download

    async def send(message):
        if message["type"] == stage:
            raise OSError("broken transport")

    async def exercise():
        with pytest.raises(ClientDisconnect):
            await response(_scope("2.4"), _receive, send)
        assert archive.closed, "ZIP must close before response.__call__ returns"

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["http.response.start", "http.response.body"])
def test_zip_closes_immediately_when_asgi_24_send_is_cancelled(download, stage):
    response, archive = download

    async def send(message):
        if message["type"] == stage:
            raise asyncio.CancelledError

    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await response(_scope("2.4"), _receive, send)
        assert archive.closed, "ZIP must close before cancellation propagates"

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["http.response.start", "http.response.body"])
def test_zip_closes_immediately_when_asgi_23_transport_raises(download, stage):
    response, archive = download

    async def send(message):
        if message["type"] == stage:
            raise OSError("broken transport")

    async def exercise():
        with pytest.raises((OSError, ExceptionGroup)) as captured:
            await response(_scope("2.3"), _receive, send)
        assert all(isinstance(error, OSError) for error in _leaves(captured.value))
        assert archive.closed, "ZIP must close before the legacy task group exits"

    asyncio.run(exercise())


@pytest.mark.parametrize("version", ["2.3", "2.4"])
def test_zip_closes_after_normal_send_and_preserves_all_bytes(download, version):
    response, archive = download
    messages = []

    async def send(message):
        messages.append(message)

    async def exercise():
        await response(_scope(version), _receive, send)
        assert archive.closed

    asyncio.run(exercise())
    assert messages[0]["type"] == "http.response.start" and messages[0]["status"] == 200
    assert b"".join(message.get("body", b"") for message in messages) == _CONTENT
    assert messages[-1]["type"] == "http.response.body" and messages[-1]["more_body"] is False
