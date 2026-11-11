"""隔离 SDK 的全局事件循环和私有连接接口；所有网络等待均可取消。"""

import asyncio
import json
import threading
from collections.abc import Callable, Mapping
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import httpx

from litgleaner.adapters.feishu.api import sdk
from litgleaner.domain.errors import AuthFailed, ConfigError, SchemaChanged, TransientError

ENDPOINT_TIMEOUT_SECONDS = 5
CONNECT_TIMEOUT_SECONDS = 5
DISCONNECT_TIMEOUT_SECONDS = 2
_SCHEMA_ERROR = -1
_SDK_OWNER = threading.Lock()


async def _endpoint(module: Any, client: Any, app_id: str, secret: str) -> str:
    """端点协议沿用 SDK；异步请求与总超时替代其无超时的 requests.post。"""
    async with httpx.AsyncClient(timeout=ENDPOINT_TIMEOUT_SECONDS) as http:
        response = await asyncio.wait_for(
            http.post(
                f"{client._domain}{module.GEN_ENDPOINT_URI}",
                headers={"locale": "zh"},
                json={"AppID": app_id, "AppSecret": secret},
            ),
            timeout=ENDPOINT_TIMEOUT_SECONDS,
        )
    if response.status_code in (401, 403):
        raise module.ClientException(response.status_code, "endpoint authentication failed")
    if response.status_code >= 500 or response.status_code == 429:
        raise module.ServerException(response.status_code, "endpoint temporarily unavailable")
    if response.status_code != 200:
        raise module.ClientException(_SCHEMA_ERROR, "unexpected endpoint HTTP status")
    try:
        payload = response.json()
        code = payload["code"]
        if type(code) is not int:
            raise ValueError("invalid code")
    except (ValueError, TypeError, KeyError):
        raise module.ClientException(_SCHEMA_ERROR, "invalid endpoint response") from None
    if code in (module.SYSTEM_BUSY, module.INTERNAL_ERROR):
        raise module.ServerException(code, "endpoint temporarily unavailable")
    if code != module.OK:
        raise module.ClientException(code, "endpoint authentication failed")
    return _endpoint_url(module, client, payload)


def _endpoint_url(module: Any, client: Any, payload: Any) -> str:
    try:
        data = payload["data"]
        url = data["URL"]
        if not isinstance(url, str):
            raise ValueError("invalid URL")
        parsed_url = urlsplit(url)
        query = parse_qs(parsed_url.query)
        service_id = query.get("service_id", [""])[0]
        if (
            parsed_url.scheme != "wss"
            or not parsed_url.hostname
            or parsed_url.fragment
            or parsed_url.username
            or not query.get("device_id")
            or not service_id.isascii()
            or not service_id.isdecimal()
        ):
            raise ValueError("invalid URL")
        if (config := data.get("ClientConfig")) is not None:
            if not isinstance(config, Mapping):
                raise ValueError("invalid connection config")
            minimums = {
                "ReconnectCount": -1,
                "ReconnectInterval": 1,
                "ReconnectNonce": 0,
                "PingInterval": 1,
            }
            for field, minimum in minimums.items():
                value = config[field]
                if type(value) is not int or value < minimum:
                    raise ValueError("invalid connection config")
        parsed = module.JSON.unmarshal(json.dumps(payload), module.EndpointResp)
        if parsed.data.ClientConfig is not None:
            client._configure(parsed.data.ClientConfig)
        return str(url)
    except (AttributeError, KeyError, TypeError, ValueError):
        raise module.ClientException(_SCHEMA_ERROR, "invalid endpoint response") from None


def _failure(module: Any, error: BaseException) -> Exception:
    if isinstance(error, module.ClientException):
        if error.code == _SCHEMA_ERROR:
            return SchemaChanged("飞书长连接端点协议不符合预期")
        return AuthFailed(f"飞书长连接认证失败（code={error.code}）")
    return TransientError("飞书长连接中断，连接任务已停止")


class ListenerRuntime:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._guard = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._main: asyncio.Task[None] | None = None
        self._failure: BaseException | None = None

    def stop(self) -> None:
        self._stop.set()
        with self._guard:
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._cancel)

    def _cancel(self) -> None:
        if self._main is not None:
            self._main.cancel()

    def run(self, app_id: str, secret: str, handler: Callable[[Any], Any]) -> None:
        if self._stop.is_set():
            return
        if not _SDK_OWNER.acquire(blocking=False):
            raise ConfigError("同一进程只能运行一个飞书长连接监听器")
        try:
            self._run_owned(app_id, secret, handler)
        finally:
            _SDK_OWNER.release()

    def _run_owned(self, app_id: str, secret: str, handler: Callable[[Any], Any]) -> None:
        previous = _thread_loop()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        module, old_loop, client = None, None, None
        try:
            module = sdk("lark_oapi.ws.client")
            old_loop = module.loop
            if old_loop is not loop and old_loop.is_running():
                raise ConfigError("飞书 SDK 的事件循环已被其他监听器占用")
            module.loop = loop
            client = _client(module, app_id, secret, handler)
            loop.set_exception_handler(self._background_failure)
            with self._guard:
                self._loop = loop
                self._main = loop.create_task(self._serve(module, client))
            if self._stop.is_set():
                self._cancel()
            self._wait(loop, module)
        finally:
            try:
                loop.run_until_complete(_cleanup(client))
            finally:
                with self._guard:
                    self._loop, self._main = None, None
                    loop.close()
                if module is not None:
                    module.loop = old_loop
                asyncio.set_event_loop(previous)

    def _wait(self, loop: asyncio.AbstractEventLoop, module: Any) -> None:
        assert self._main is not None
        try:
            loop.run_until_complete(self._main)
        except asyncio.CancelledError:
            if not self._stop.is_set():
                raise _failure(module, self._failure or RuntimeError()) from None
        except Exception as error:
            raise _failure(module, error) from None

    async def _serve(self, module: Any, client: Any) -> None:
        try:
            await client._connect()
        except module.ClientException:
            raise
        except Exception:
            await asyncio.wait_for(client._disconnect(), DISCONNECT_TIMEOUT_SECONDS)
            await client._reconnect()
        asyncio.get_running_loop().create_task(client._ping_loop())
        await asyncio.Future[None]()

    def _background_failure(
        self, _loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        self._failure = context.get("exception") or RuntimeError("connection task failed")
        self._cancel()


def _thread_loop() -> asyncio.AbstractEventLoop | None:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return None
    if loop.is_running():
        raise ConfigError("飞书长连接监听必须在独立的同步线程中启动")
    return loop


def _client(module: Any, app_id: str, secret: str, handler: Callable[[Any], Any]) -> Any:
    lark = sdk()
    event_handler = (
        lark.EventDispatcherHandler.builder("", "").register_p2_card_action_trigger(handler).build()
    )
    try:
        client = module.Client(
            app_id, secret, event_handler=event_handler, log_level=lark.LogLevel.WARNING
        )
        for name in ("_connect", "_disconnect", "_reconnect", "_ping_loop", "_configure"):
            if not callable(getattr(client, name, None)):
                raise TypeError("incompatible SDK")
        original = client._connect
    except (AttributeError, TypeError):
        raise ConfigError("飞书 SDK 的长连接接口不兼容，请检查依赖版本") from None

    async def connect() -> None:
        url = await _endpoint(module, client, app_id, secret)
        client._get_conn_url = lambda: url
        await asyncio.wait_for(original(), timeout=CONNECT_TIMEOUT_SECONDS)

    client._connect = connect
    return client


async def _cleanup(client: Any) -> None:
    loop = asyncio.get_running_loop()
    if client is not None:
        client._auto_reconnect = False
    tasks = asyncio.all_tasks(loop) - {asyncio.current_task(loop)}
    pending: set[asyncio.Task[Any]] = set()
    for task in tasks:
        task.cancel()
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=1)
        for task in pending:
            task.cancel()
        if pending:
            ended, pending = await asyncio.wait(pending, timeout=1)
            done |= ended
        await asyncio.gather(*done, return_exceptions=True)
    if client is not None:
        connection = client._conn
        try:
            await asyncio.wait_for(client._disconnect(), timeout=DISCONNECT_TIMEOUT_SECONDS)
        except (Exception, asyncio.CancelledError):
            if connection is not None and getattr(connection, "transport", None) is not None:
                connection.transport.abort()
    await loop.shutdown_asyncgens()
    await cast(asyncio.BaseEventLoop, loop).shutdown_default_executor(timeout=1)
    if pending:
        raise ConfigError("飞书 SDK 的后台任务未能在期限内退出，请检查依赖版本")
