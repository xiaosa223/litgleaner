"""飞书开放平台的消息接口：发卡片、更新卡片。用官方 SDK lark-oapi。

发送带 uuid：飞书对同一 uuid 一小时内只发一次，所以连接断开后的重试不会重复推送。
发布阶段的前几次重试（默认 300 秒、600 秒后）都落在这个窗口里。
错误码按开放平台文档的常见值分类（记忆值，未逐个核实）；没列出的一律当作确定没发出、不可重试。
"""

import hashlib
import importlib
import logging
from typing import Any

from litfactory.domain.errors import (
    AuthFailed,
    ConfigError,
    PermanentError,
    RateLimited,
    TransientError,
)

logger = logging.getLogger(__name__)

APP_ID_ENV = "FEISHU_APP_ID"
APP_SECRET_ENV = "FEISHU_APP_SECRET"
RECEIVE_ID_TYPES = frozenset({"open_id", "user_id", "union_id", "email", "chat_id"})
# 凭据错误、应用没有对应权限。
_AUTH_CODES = frozenset({10014, 99991661, 99991663, 99991664, 99991672, 99991679})
# 频率限制。
_RATE_CODES = frozenset({99991400, 230020})
_RATE_RETRY_SECONDS = 60


def sdk(module: str = "lark_oapi") -> Any:
    """lark-oapi 是可选依赖（litfactory[feishu]），而且导入很慢、导入时会建事件循环，
    所以用到飞书时才加载。没装时其他渠道照常可用。"""
    try:
        return importlib.import_module(module)
    except ImportError as error:
        raise ConfigError("飞书渠道需要安装可选依赖：uv sync --extra feishu") from error


def card_response(content: dict[str, Any]) -> Any:
    model = sdk("lark_oapi.event.callback.model.p2_card_action_trigger")
    return model.P2CardActionTriggerResponse(content)


def send_uuid(content: str) -> str:
    """同一份卡片内容得到同一个 uuid，重试时飞书据此去重。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]


def check_response(response: Any, action: str) -> None:
    if response.success():
        return
    code = int(getattr(response, "code", 0) or 0)
    text = f"飞书{action}失败：code={code} {getattr(response, 'msg', '')}"
    log_id = response.get_log_id() if hasattr(response, "get_log_id") else None
    if log_id:
        text += f"（log_id {log_id}）"
    if code in _AUTH_CODES:
        raise AuthFailed(f"{text}。检查 {APP_ID_ENV}/{APP_SECRET_ENV} 和应用权限")
    if code in _RATE_CODES:
        raise RateLimited(text, _RATE_RETRY_SECONDS)
    raise PermanentError(text)


class FeishuMessenger:
    def __init__(
        self, app_id: str, app_secret: str, *, timeout: float = 30, client: Any = None
    ) -> None:
        if client is None:
            if not app_id or not app_secret:
                raise ConfigError(f"飞书渠道需要环境变量 {APP_ID_ENV} 和 {APP_SECRET_ENV}")
            lark = sdk()
            client = (
                lark.Client.builder()
                .app_id(app_id)
                .app_secret(app_secret)
                .timeout(timeout)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
        self._client = client

    def send(self, receive_id_type: str, receive_id: str, content: str) -> str:
        """返回 message_id。确定没发出时抛 TransientError / PermanentError，读超时等原样抛出。"""
        im = sdk("lark_oapi.api.im.v1")
        body = (
            im.CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("interactive")
            .content(content)
            .uuid(send_uuid(content))
            .build()
        )
        request = (
            im.CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        response = self._call(lambda: self._client.im.v1.message.create(request))
        check_response(response, "发送卡片")
        message_id = getattr(response.data, "message_id", None)
        if not message_id:
            raise PermanentError("飞书发送成功但没有返回 message_id")
        return str(message_id)

    def update(self, message_id: str, content: str) -> None:
        im = sdk("lark_oapi.api.im.v1")
        request = (
            im.PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(im.PatchMessageRequestBody.builder().content(content).build())
            .build()
        )
        response = self._call(lambda: self._client.im.v1.message.patch(request))
        check_response(response, "更新卡片")

    @staticmethod
    def _call(request: Any) -> Any:
        requests = sdk("requests")
        try:
            return request()
        except requests.exceptions.ReadTimeout:
            raise  # 请求可能已经到达飞书，结果未知，交给人工确认
        except requests.exceptions.ConnectionError as error:
            # 同一 uuid 一小时内重试不会重复推送，所以连接类错误可以重试。
            raise TransientError(f"连接飞书失败：{type(error).__name__}") from error
