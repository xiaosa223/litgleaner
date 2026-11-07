"""飞书卡片按钮回调：通过长连接接收，本机不需要公网地址。

一个飞书应用的卡片回调只会送到一个长连接客户端。不要和其他程序（比如 agent-autoloop
的飞书通道）共用同一个应用，否则点击会被对方收走。

回调必须在 3 秒内答复，所以这里只核验身份和格式，把点击交给 on_click 排队，立刻回一个提示。
写入文献库、更新卡片由调用方在后台完成。
"""

import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from litfactory.adapters.feishu.api import card_response, sdk
from litfactory.domain.errors import ConfigError
from litfactory.domain.library import decode_action
from litfactory.ports.inbound import CardClick

logger = logging.getLogger(__name__)

TOAST_SAVE = "已收到，正在加入 Zotero"
TOAST_DISMISS = "已标记为不感兴趣"
TOAST_FORBIDDEN = "只有订阅者本人可以操作这张卡片"
TOAST_INVALID = "这个按钮已经失效"


def answerers_from(options: Mapping[str, Any]) -> frozenset[str]:
    """谁的点击算数。发给个人时默认就是本人；发到群里必须写明。"""
    configured = options.get("answerers")
    if configured:
        if isinstance(configured, str) or not isinstance(configured, Iterable):
            raise ConfigError("飞书 answerers 要写成 open_id 列表")
        return frozenset(str(item) for item in configured)
    if str(options.get("receive_id_type") or "open_id") == "open_id" and options.get("receive_id"):
        return frozenset({str(options["receive_id"])})
    raise ConfigError("飞书渠道发到群里时，需要在 options.answerers 写上可以操作的 open_id")


def parse_click(payload: Any, answerers: frozenset[str]) -> CardClick | str:
    """返回点击；不能接受时返回给用户看的提示。payload 是 SDK 的 P2CardActionTrigger。"""
    event = getattr(payload, "event", None)
    operator = getattr(getattr(event, "operator", None), "open_id", None)
    if not operator or operator not in answerers:
        return TOAST_FORBIDDEN
    action = decode_action(getattr(getattr(event, "action", None), "value", None))
    event_id = getattr(getattr(payload, "header", None), "event_id", None)
    if action is None or not event_id:
        return TOAST_INVALID
    message_id = getattr(getattr(event, "context", None), "open_message_id", None)
    return CardClick(action, str(operator), str(event_id), message_id)


def toast_for(result: CardClick | str) -> dict[str, Any]:
    if isinstance(result, str):
        return {"toast": {"type": "warning", "content": result}}
    text = TOAST_SAVE if result.action.action == "save_to_library" else TOAST_DISMISS
    return {"toast": {"type": "info", "content": text}}


class FeishuListener:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        answerers: frozenset[str],
        on_click: Callable[[CardClick], None],
    ) -> None:
        if not app_id or not app_secret:
            raise ConfigError("飞书监听需要环境变量 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        self._app_id = app_id
        self._app_secret = app_secret
        self._answerers = answerers
        self._on_click = on_click

    def handle(self, payload: Any) -> dict[str, Any]:
        """处理一次回调，返回飞书要求的答复内容。任何异常都不能让长连接断掉。"""
        result = parse_click(payload, self._answerers)
        if isinstance(result, CardClick):
            try:
                self._on_click(result)
            except Exception:
                logger.exception("处理飞书点击 %s 失败", result.event_id)
                return {"toast": {"type": "error", "content": "处理失败，稍后再试"}}
        else:
            logger.warning("忽略飞书点击：%s", result)
        return toast_for(result)

    def start(self) -> None:
        """阻塞运行，断线自动重连。"""
        lark = sdk()

        def callback(payload: Any) -> Any:
            return card_response(self.handle(payload))

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_card_action_trigger(callback)
            .build()
        )
        client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.WARNING,
        )
        client.start()
