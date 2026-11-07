"""处理发送结果未知或失败的推送。系统不替你猜，由人确认后再决定。"""

from litgleaner.application.context import AppContext
from litgleaner.application.stages.publish import publish_job_key
from litgleaner.domain.errors import ConfigError


def unresolved_deliveries(ctx: AppContext) -> tuple[tuple[str, str], ...]:
    return ctx.store.outputs.unresolved()


def resolve_delivery(ctx: AppContext, digest_id: str, channel: str, resend: bool) -> None:
    """resend=False 表示已确认对方收到；resend=True 表示确认没收到，下次运行重发。"""
    status = ctx.store.outputs.delivery_status(digest_id, channel)
    if status is None:
        raise ConfigError(f"推送记录不存在：{digest_id} {channel}")
    if status == "sent":
        raise ConfigError(f"这条推送已经发送成功，无需处理：{digest_id} {channel}")
    now = ctx.now_iso()
    key = publish_job_key(digest_id, channel)
    with ctx.store.atomic():
        if resend:
            ctx.store.outputs.set_delivery(digest_id, channel, "failed", now)
            ctx.store.jobs.reopen(key, now)
        else:
            ctx.store.outputs.set_delivery(digest_id, channel, "sent", now)
            ctx.store.jobs.mark_done(key, now)
