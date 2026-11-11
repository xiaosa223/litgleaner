"""综述骨架保留首写完整附件；读取只返回冻结历史。"""

import json

from sqlalchemy import func, select, text
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.language import LanguageCode
from litgleaner.domain.outline import TopicOutline, TopicOutlineSettings
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import topic_outline


class SqlOutlineRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def add(self, result: TopicOutline) -> TopicOutline:
        result = TopicOutline.model_validate(result.model_dump())
        names = ("id", "topic_id", "topic_hash", "input_hash", "producer_version", "created_at")
        if any(not getattr(result, name).strip() for name in (*names, "model", "scope_id")):
            raise ValueError("topic_outline_identifier_empty")
        with self._db.atomic():
            existing = self.by_fingerprint(
                result.topic_id, result.topic_hash, result.input_hash, result.producer_version
            )
            if existing is not None:
                return existing
            self._check_papers(result.considered_paper_ids)
            self._db.write(
                insert(topic_outline).values(
                    **result.model_dump(include=set(names)), data=result.model_dump_json()
                )
            )
        return result

    def get(self, outline_id: str) -> TopicOutline | None:
        rows = self._db.fetch(select(topic_outline.c.data).where(topic_outline.c.id == outline_id))
        return TopicOutline.model_validate_json(rows[0].data) if rows else None

    def by_fingerprint(
        self, topic_id: str, topic_hash: str, input_hash: str, producer_version: str
    ) -> TopicOutline | None:
        rows = self._db.fetch(
            select(topic_outline.c.data).where(
                topic_outline.c.topic_id == topic_id,
                topic_outline.c.topic_hash == topic_hash,
                topic_outline.c.input_hash == input_hash,
                topic_outline.c.producer_version == producer_version,
            )
        )
        return TopicOutline.model_validate_json(rows[0].data) if rows else None

    def latest(self, topic_id: str) -> TopicOutline | None:
        history = self.history(topic_id, limit=1)
        return history[0] if history else None

    def history(self, topic_id: str, limit: int = 20) -> tuple[TopicOutline, ...]:
        if type(limit) is not int or limit < 1:
            raise ValueError("topic_outline_limit_invalid")
        rows = self._db.fetch(
            select(topic_outline.c.data)
            .where(topic_outline.c.topic_id == topic_id)
            .order_by(topic_outline.c.created_at.desc(), topic_outline.c.id.desc())
            .limit(limit)
        )
        return tuple(TopicOutline.model_validate_json(row.data) for row in rows)

    def compatible(
        self,
        topic_id: str,
        topic_hash: str,
        producer_version: str,
        reading_language: LanguageCode,
        token_budget: int,
    ) -> tuple[TopicOutline, ...]:
        TopicOutlineSettings(token_budget=token_budget)
        if reading_language not in ("zh-CN", "en"):
            raise ValueError("topic_outline_language_invalid")
        rows = self._db.fetch(
            select(topic_outline.c.data)
            .where(
                topic_outline.c.topic_id == topic_id,
                topic_outline.c.topic_hash == topic_hash,
                topic_outline.c.producer_version == producer_version,
                func.json_extract(topic_outline.c.data, "$.reading_language") == reading_language,
                func.json_extract(topic_outline.c.data, "$.token_budget") == token_budget,
            )
            .order_by(topic_outline.c.created_at.desc(), topic_outline.c.id.desc())
        )
        return tuple(TopicOutline.model_validate_json(row.data) for row in rows)

    def _check_papers(self, paper_ids: tuple[str, ...]) -> None:
        rows = self._db.fetch(
            text(
                "SELECT item.value FROM json_each(:ids) item LEFT JOIN paper p ON p.id=item.value "
                "WHERE p.id IS NULL LIMIT 1"
            ).bindparams(ids=json.dumps(paper_ids))
        )
        if rows:
            raise ValueError("topic_outline_paper_missing")
