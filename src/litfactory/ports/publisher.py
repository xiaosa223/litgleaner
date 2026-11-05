"""发布接口。渲染和发送分开，选文和内容与渠道无关。"""

from dataclasses import dataclass
from typing import Protocol

from litfactory.domain.digest import DigestView


@dataclass(frozen=True)
class Rendered:
    title: str
    body: str


@dataclass(frozen=True)
class Receipt:
    external_id: str


class Renderer(Protocol):
    def render(self, view: DigestView) -> Rendered: ...


class Publisher(Protocol):
    """确定没有发出去时才抛 TransientError 或 PermanentError，之后可以重试。

    超时、连接中断这类无法确定对方是否收到的情况，不能归为这两种，否则可能重复推送。
    其他异常都视为结果未知，不会自动重发，由人工确认。
    """

    def publish(self, rendered: Rendered) -> Receipt: ...
