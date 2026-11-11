"""入站点击的业务核验与执行；调用方负责队列及共享运行锁。"""

import logging
from collections.abc import Mapping

from litgleaner.application.context import AppContext
from litgleaner.application.library import record_action
from litgleaner.application.pipeline import run_pipeline
from litgleaner.application.stages.publish import rerender
from litgleaner.ports.inbound import CardClick
from litgleaner.ports.publisher import Rendered

logger = logging.getLogger(__name__)


def click_channel(
    ctx: AppContext, click: CardClick, answerers: Mapping[str, frozenset[str]]
) -> str | None:
    """按原推送确定渠道并重新核验订阅人；旧卡片只接受无歧义的单渠道。"""
    if click.action.digest_id:
        digest = ctx.store.outputs.get_digest(click.action.digest_id)
        if digest is None:
            return None
        channel = digest.channel
        cards = ctx.store.outputs.digest_cards(digest.id)
        if not any(card.get("paper_id") == click.action.paper_id for card in cards):
            return None
    elif len(answerers) == 1:
        channel = next(iter(answerers))
    else:
        return None
    if channel not in ctx.channels or click.actor not in answerers.get(channel, frozenset()):
        return None
    return channel


def apply_click(
    ctx: AppContext, click: CardClick, answerers: Mapping[str, frozenset[str]]
) -> Rendered | None:
    """记录反馈并只执行文献库阶段；返回重绘内容，由调用方在释放锁后更新消息。"""
    channel = click_channel(ctx, click, answerers)
    if channel is None:
        logger.warning("忽略无法核验原推送或订阅人的点击 %s", click.event_id)
        return None
    record_action(ctx, click.action, channel=channel, actor=click.actor, external_id=click.event_id)
    if click.action.action == "save_to_library" and ctx.library is not None:
        report = run_pipeline(ctx, only="library")
        if not report.ok:
            logger.warning("文献库操作留待重试：%s", report.summary())
    if click.message_id and click.action.digest_id:
        return rerender(ctx, click.action.digest_id, channel)
    return None
