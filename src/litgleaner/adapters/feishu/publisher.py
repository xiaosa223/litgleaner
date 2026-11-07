"""飞书发布适配器：把渲染好的卡片发给一个人或一个群。"""

from collections.abc import Mapping
from typing import Any

from litgleaner.adapters.feishu.api import RECEIVE_ID_TYPES, FeishuMessenger
from litgleaner.domain.errors import ConfigError
from litgleaner.ports.publisher import Receipt, Rendered


class FeishuPublisher:
    def __init__(self, options: Mapping[str, Any], messenger: FeishuMessenger) -> None:
        self.receive_id = str(options.get("receive_id") or "")
        self.receive_id_type = str(options.get("receive_id_type") or "open_id")
        if not self.receive_id:
            raise ConfigError("飞书渠道需要 options.receive_id（你的 open_id 或群的 chat_id）")
        if self.receive_id_type not in RECEIVE_ID_TYPES:
            raise ConfigError(
                f"飞书 receive_id_type 只能是 {sorted(RECEIVE_ID_TYPES)}，"
                f"不能是 {self.receive_id_type}"
            )
        self._messenger = messenger

    def publish(self, rendered: Rendered) -> Receipt:
        message_id = self._messenger.send(self.receive_id_type, self.receive_id, rendered.body)
        return Receipt(external_id=message_id)
