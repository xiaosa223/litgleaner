"""每个 HTTP 请求固定一个运行时快照，包含整个 SSE 生命周期。"""

from collections.abc import Callable
from typing import Annotated, cast

from fastapi import Depends, Request

from litgleaner.application.context import AppContext


def request_context(request: Request) -> AppContext:
    provider = cast(Callable[[], AppContext], request.app.state.context_provider)
    return provider()


Context = Annotated[AppContext, Depends(request_context)]
