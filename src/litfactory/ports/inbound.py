"""入站接口：渠道上的按钮点击。渠道只负责核验身份和格式，记录和处理在流程里完成。"""

from dataclasses import dataclass

from litfactory.domain.library import CardAction


@dataclass(frozen=True)
class CardClick:
    action: CardAction
    actor: str  # 渠道里的用户标识，已经过白名单核验
    event_id: str  # 渠道给的事件编号，同一次点击重复送达时相同
    message_id: str | None  # 被点击的那条消息，用于之后更新卡片
