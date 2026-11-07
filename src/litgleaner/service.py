"""常驻服务（第 15.5 节）：一个进程里组合领读器接口和定时调度。

- 只监听 127.0.0.1，端口默认随机；每次启动生成新的启动令牌。
- 调度和命令行共用同一把运行锁：命令行正在跑流程时，本轮直接跳过，下一轮再试。
- 飞书长连接目前仍由 litgleaner feishu listen 单独运行，之后并入这里。
"""

import html
import logging
import os
import socket
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

import uvicorn

from litgleaner.application.context import AppContext
from litgleaner.application.pipeline import RunReport, run_pipeline
from litgleaner.cli_shared import try_run_lock
from litgleaner.infra.config import LoadedConfig
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity

logger = logging.getLogger(__name__)

DEFAULT_STATIC_DIR = Path(__file__).parent / "server" / "static"
DEFAULT_INTERVAL_SECONDS = 300
_JOIN_TIMEOUT_SECONDS = 10


def run_due(config: LoadedConfig, ctx: AppContext) -> RunReport | None:
    """跑一轮流水线。拿不到运行锁时返回 None。到推送时间才会推送，所以每轮都跑是安全的。"""
    with try_run_lock(config) as acquired:
        if not acquired:
            logger.info("另一个流程正在运行，本轮调度跳过")
            return None
        report = run_pipeline(ctx)
    log = logger.info if report.ok else logger.warning
    log("定时运行完成：%s", report.summary().replace("\n", "；"))
    return report


def open_in_browser(url: str, directory: Path) -> Path:
    """通过只有本人可读的跳转页打开启动链接。

    直接把链接交给浏览器时，令牌会出现在 open 等子进程的命令行参数里，本机其他用户用 ps 就能看到。
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / "reader-launch.html").resolve()
    escaped = html.escape(url, quote=True)
    page = (
        '<!doctype html><meta charset="utf-8">'
        f'<meta http-equiv="refresh" content="0;url={escaped}">'
        f'<a href="{escaped}">打开拾穗领读器</a>'
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(page)
    path.chmod(0o600)  # 文件已存在时 os.open 不会改权限
    webbrowser.open(path.as_uri())
    return path


class Service:
    def __init__(
        self,
        config: LoadedConfig,
        ctx: AppContext,
        *,
        static_dir: Path | None = DEFAULT_STATIC_DIR,
        port: int = 0,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._config = config
        self._ctx = ctx
        self._static_dir = static_dir
        self._port = port
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._server: uvicorn.Server | None = None
        self._security: LocalSecurity | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._require_security().port}"

    @property
    def launch_url(self) -> str:
        return self._require_security().launch_url

    def _require_security(self) -> LocalSecurity:
        if self._security is None:
            raise RuntimeError("服务还没有启动")
        return self._security

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name == "nt":  # Windows 上 SO_REUSEADDR 允许别的进程抢占同一端口
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self._port))
        self._security = LocalSecurity(port=sock.getsockname()[1])
        app = create_app(self._ctx, self._security, self._static_dir)
        settings = uvicorn.Config(
            app,
            log_level="warning",
            lifespan="off",
            access_log=False,
            server_header=False,
            proxy_headers=False,
        )
        self._server = uvicorn.Server(settings)
        server = self._server
        self._spawn("litgleaner-http", lambda: server.run(sockets=[sock]))
        self._spawn("litgleaner-scheduler", self._schedule)
        self._wait_until_listening()

    def _spawn(self, name: str, target: Callable[[], object]) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _wait_until_listening(self) -> None:
        server = self._server
        for _ in range(200):
            if server is not None and server.started:
                return
            if self._stop.wait(0.05):
                return
        raise RuntimeError("领读器接口没有在 10 秒内启动")

    def _schedule(self) -> None:
        while not self._stop.is_set():
            try:
                run_due(self._config, self._ctx)
            except Exception:  # 调度线程不能因为一次失败退出，详情写进日志
                logger.exception("定时运行出错")
            self._stop.wait(self._interval)

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.should_exit = True
        for thread in self._threads:
            thread.join(_JOIN_TIMEOUT_SECONDS)
        self._threads.clear()

    def wait(self) -> None:
        """阻塞到 Ctrl-C。"""
        try:
            while not self._stop.wait(1):
                pass
        except KeyboardInterrupt:
            logger.info("收到中断，正在停止")
        finally:
            self.stop()
