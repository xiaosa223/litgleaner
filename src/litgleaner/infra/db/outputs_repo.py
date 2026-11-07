"""输出层：Digest 和发送状态。"""

import json
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import select, update

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import delivery, digest, digest_item
from litgleaner.ports.store import DeliveryStatus, DigestRecord

_UNFINISHED = ("pending", "failed", "sending")


class SqlOutputRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def digest_exists(self, route_id: str, topic_id: str, period: str) -> bool:
        rows = self._db.fetch(
            select(digest.c.id).where(
                digest.c.route_id == route_id,
                digest.c.topic_id == topic_id,
                digest.c.period == period,
            )
        )
        return bool(rows)

    def create_digest(
        self, record: DigestRecord, items: Iterable[tuple[str, Mapping[str, Any]]], now: str
    ) -> None:
        with self._db.atomic():
            self._db.write(
                digest.insert().values(
                    id=record.id,
                    route_id=record.route_id,
                    topic_id=record.topic_id,
                    channel=record.channel,
                    period=record.period,
                    created_at=now,
                )
            )
            for position, (paper_id, card) in enumerate(items):
                self._db.write(
                    digest_item.insert().values(
                        digest_id=record.id,
                        position=position,
                        paper_id=paper_id,
                        card=json.dumps(card, ensure_ascii=False, sort_keys=True),
                    )
                )
            self._db.write(
                delivery.insert().values(
                    digest_id=record.id, channel=record.channel, status="pending", updated_at=now
                )
            )

    def get_digest(self, digest_id: str) -> DigestRecord | None:
        rows = self._db.fetch(select(digest).where(digest.c.id == digest_id))
        if not rows:
            return None
        row = rows[0]
        return DigestRecord(row.id, row.route_id, row.topic_id, row.channel, row.period)

    def digest_cards(self, digest_id: str) -> tuple[Mapping[str, Any], ...]:
        rows = self._db.fetch(
            select(digest_item.c.card)
            .where(digest_item.c.digest_id == digest_id)
            .order_by(digest_item.c.position)
        )
        return tuple(json.loads(row.card) for row in rows)

    def undelivered(self) -> tuple[tuple[str, str], ...]:
        rows = self._db.fetch(
            select(delivery.c.digest_id, delivery.c.channel)
            .where(delivery.c.status.in_(_UNFINISHED))
            .order_by(delivery.c.digest_id)
        )
        return tuple((row.digest_id, row.channel) for row in rows)

    def unresolved(self) -> tuple[tuple[str, str], ...]:
        rows = self._db.fetch(
            select(delivery.c.digest_id, delivery.c.channel)
            .where(delivery.c.status.in_(("sending", "failed")))
            .order_by(delivery.c.digest_id)
        )
        return tuple((row.digest_id, row.channel) for row in rows)

    def claim_delivery(self, digest_id: str, channel: str, now: str) -> bool:
        """原子地占用这次发送。占用失败说明正在发送，或上次结果未知。"""
        claimed = self._db.write(
            update(delivery)
            .where(
                delivery.c.digest_id == digest_id,
                delivery.c.channel == channel,
                delivery.c.status.in_(("pending", "failed")),
            )
            .values(status="sending", last_error=None, updated_at=now)
        )
        return claimed == 1

    def delivery_status(self, digest_id: str, channel: str) -> str | None:
        rows = self._db.fetch(
            select(delivery.c.status).where(
                delivery.c.digest_id == digest_id, delivery.c.channel == channel
            )
        )
        return rows[0].status if rows else None

    def set_delivery(
        self,
        digest_id: str,
        channel: str,
        status: DeliveryStatus,
        now: str,
        *,
        external_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self._db.write(
            update(delivery)
            .where(delivery.c.digest_id == digest_id, delivery.c.channel == channel)
            .values(status=status, external_id=external_id, last_error=error, updated_at=now)
        )
