"""本地安全（第 15.5 节）。

只认本机地址，启动链接换会话，检查 Host 和 Origin，状态变更带 CSRF 令牌。
"""

import hmac
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

SESSION_COOKIE = "litgleaner_session"
CSRF_HEADER = "x-csrf-token"
LAUNCH_PATH = "/launch"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_LOOPBACK_NAMES = ("127.0.0.1", "localhost")
MAX_BODY_BYTES = 64 * 1024  # 批注上限 2 万字，UTF-8 下也远小于这个值
UPLOAD_PATH = "/api/inbox/files"
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 用户上传的 PDF（第 12.8 节）
DOCUMENT_PATH = "/api/topics/profile-from-document"
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024  # 推断研究画像的任务书等文档（ADR 0012）
_MAX_SESSIONS = 64
_IDLE_SECONDS = 30 * 24 * 3600
# 模型输出一律按纯文本渲染，页面不需要内联脚本；PDF 引擎的 worker、wasm 和字体来自本站。
# PDF 引擎是 PDFium 的 wasm，浏览器编译 wasm 需要 'wasm-unsafe-eval'；它不放行 eval 和内联脚本。
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self' 'wasm-unsafe-eval'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "worker-src 'self' blob:",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)


class LocalSecurity:
    """启动令牌每次启动重新生成；会话只存在内存里，服务重启后要重新从启动链接进入。"""

    def __init__(
        self,
        port: int,
        launch_token: str | None = None,
        *,
        max_sessions: int = _MAX_SESSIONS,
        idle_seconds: float = _IDLE_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.port = port
        self.launch_token = launch_token or secrets.token_urlsafe(32)
        # 会话 → (CSRF 令牌, 最近使用时间)，最久没用的在前；超过上限或闲置太久就作废
        self._sessions: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._max_sessions = max_sessions
        self._idle_seconds = idle_seconds
        self._now = now
        self._lock = threading.Lock()

    @property
    def launch_url(self) -> str:
        return f"http://127.0.0.1:{self.port}{LAUNCH_PATH}?token={self.launch_token}"

    def host_ok(self, host: str | None) -> bool:
        return host in {f"{name}:{self.port}" for name in _LOOPBACK_NAMES}

    def origin_ok(self, origin: str | None) -> bool:
        """跨站请求会带上别的 Origin。没有 Origin 的请求（地址栏打开、同源 GET）放行。"""
        if origin is None:
            return True
        return origin in {f"http://{name}:{self.port}" for name in _LOOPBACK_NAMES}

    def launch_ok(self, token: str | None) -> bool:
        return token is not None and hmac.compare_digest(token, self.launch_token)

    def new_session(self) -> str:
        session = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session] = (secrets.token_urlsafe(32), self._now())
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
        return session

    def csrf_token(self, session: str | None) -> str | None:
        """会话有效时返回它的 CSRF 令牌，并刷新最近使用时间。"""
        if session is None:
            return None
        with self._lock:
            entry = self._sessions.get(session)
            if entry is None:
                return None
            now = self._now()
            if now - entry[1] > self._idle_seconds:
                del self._sessions[session]
                return None
            self._sessions[session] = (entry[0], now)
            self._sessions.move_to_end(session)
            return entry[0]

    def csrf_ok(self, session: str | None, token: str | None) -> bool:
        expected = self.csrf_token(session)
        return expected is not None and token is not None and hmac.compare_digest(token, expected)


def _deny(request: Request, status: int, message: str) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=status)
    return PlainTextResponse(message, status_code=status)


def _body_problem(request: Request) -> Response | None:
    """请求体在解析前限制大小。没有 Content-Length 的分块上传直接拒绝。"""
    length = request.headers.get("content-length")
    if length is None:
        if "transfer-encoding" in request.headers:
            return _deny(request, 411, "需要 Content-Length")
        return None
    limits = {UPLOAD_PATH: MAX_UPLOAD_BYTES, DOCUMENT_PATH: MAX_DOCUMENT_BYTES}
    limit = limits.get(request.url.path, MAX_BODY_BYTES)
    if not length.isdigit() or int(length) > limit:
        return _deny(request, 413, "请求内容过大")
    return None


def _source_problem(security: LocalSecurity, request: Request) -> Response | None:
    """请求是否来自本机页面：防 DNS 重绑定和跨站请求，并限制请求体大小。"""
    if not security.host_ok(request.headers.get("host")):
        return _deny(request, 400, "Host 不被允许：领读器只接受本机地址")
    if not security.origin_ok(request.headers.get("origin")):
        return _deny(request, 403, "拒绝跨站请求")
    if request.method not in _SAFE_METHODS:
        return _body_problem(request)
    return None


def _session_problem(security: LocalSecurity, request: Request) -> Response | None:
    session = request.cookies.get(SESSION_COOKIE)
    if security.csrf_token(session) is None:
        return _deny(request, 401, "请用 litgleaner serve 输出的启动链接打开领读器")
    if request.method not in _SAFE_METHODS and not security.csrf_ok(
        session, request.headers.get(CSRF_HEADER)
    ):
        return _deny(request, 403, "缺少或错误的 CSRF 令牌")
    return None


def _check(security: LocalSecurity, request: Request) -> Response | None:
    problem = _source_problem(security, request)
    if problem or request.url.path == LAUNCH_PATH:
        return problem
    return _session_problem(security, request)


def install_guard(app: FastAPI, security: LocalSecurity) -> None:
    @app.middleware("http")
    async def guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = _check(security, request) or await call_next(request)
        # 个别接口（如 PDF）设置了更严格的策略时保留
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"  # 启动链接里的令牌不能通过 Referer 外泄
        if request.url.path.startswith("/api/") or request.url.path == LAUNCH_PATH:
            response.headers["Cache-Control"] = "no-store"
        return response
