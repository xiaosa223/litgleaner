"""模拟 HTTP 服务：按 URL 路由返回录制的响应，并记录收到的请求。"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).parents[1] / "fixtures"

Handler = Callable[[httpx.Request], httpx.Response]


def fixture_bytes(*parts: str) -> bytes:
    return FIXTURES.joinpath(*parts).read_bytes()


def fixture_json(*parts: str) -> Any:
    return json.loads(fixture_bytes(*parts))


@dataclass
class Router:
    """路由按顺序匹配：第一个 URL 包含 needle 的规则生效。没有匹配时返回 404。"""

    rules: list[tuple[str, Handler]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def on(self, needle: str, handler: Handler) -> "Router":
        self.rules.append((needle, handler))
        return self

    def reply(
        self,
        needle: str,
        status: int = 200,
        *,
        body: bytes = b"",
        json_body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> "Router":
        def handler(_: httpx.Request) -> httpx.Response:
            if json_body is not None:
                return httpx.Response(status, json=json_body, headers=headers)
            return httpx.Response(status, content=body, headers=headers)

        return self.on(needle, handler)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for needle, handler in self.rules:
            if needle in str(request.url):
                return handler(request)
        return httpx.Response(404, json={"error": "not routed"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def params(self, index: int = -1) -> dict[str, str]:
        return dict(self.requests[index].url.params)


def public_resolver(host: str) -> list[str]:
    """测试用 DNS：除了明确写成内网地址的主机，都解析到公网地址。"""
    return [host] if host[:1].isdigit() or ":" in host else ["93.184.216.34"]
