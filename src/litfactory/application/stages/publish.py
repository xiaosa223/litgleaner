"""发布阶段：宁可告警，也不重复推送。"""

from collections.abc import Iterable, Mapping
from typing import Any

from litfactory.application.cards import card_from_dict
from litfactory.application.context import AppContext, ChannelBinding
from litfactory.application.stages.base import Stage
from litfactory.domain.digest import DigestView, PaperCard, PublicCard, to_public_card
from litfactory.domain.errors import PermanentError, TransientError
from litfactory.ports.store import DigestRecord, JobSpec


def _view(ctx: AppContext, digest: DigestRecord, channel: ChannelBinding) -> DigestView:
    topic = ctx.topics.get(digest.topic_id)
    cards: list[PaperCard | PublicCard] = [
        card_from_dict(data) for data in ctx.store.outputs.digest_cards(digest.id)
    ]
    if channel.visibility == "public":
        cards = [to_public_card(card) for card in cards if isinstance(card, PaperCard)]
    return DigestView(
        digest_id=digest.id,
        topic_name=topic.name if topic else digest.topic_id,
        period=digest.period,
        visibility=channel.visibility,
        cards=tuple(cards),
    )


def publish_job_key(digest_id: str, channel: str) -> str:
    return f"publish:{digest_id}:{channel}"


class PublishStage(Stage):
    name = "publish"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        for digest_id, channel in ctx.store.outputs.undelivered():
            if channel in ctx.channels:
                yield JobSpec(
                    key=publish_job_key(digest_id, channel),
                    stage=self.name,
                    payload={"digest_id": digest_id, "channel": channel},
                )

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        digest_id, name = payload["digest_id"], payload["channel"]
        status = ctx.store.outputs.delivery_status(digest_id, name)
        if status == "sent":
            return
        digest = ctx.store.outputs.get_digest(digest_id)
        if digest is None:
            raise ValueError(f"Digest 不存在：{digest_id}")
        channel = ctx.channels[name]
        rendered = channel.renderer.render(_view(ctx, digest, channel))
        if not ctx.store.outputs.claim_delivery(digest_id, name, ctx.now_iso()):
            raise PermanentError(
                f"{name} 上次发送结果未知，未自动重发。确认后执行 litfactory delivery resolve"
            )
        try:
            receipt = channel.publisher.publish(rendered)
        except (TransientError, PermanentError) as error:
            ctx.store.outputs.set_delivery(
                digest_id, name, "failed", ctx.now_iso(), error=str(error)[:500]
            )
            raise
        ctx.store.outputs.set_delivery(
            digest_id, name, "sent", ctx.now_iso(), external_id=receipt.external_id
        )
