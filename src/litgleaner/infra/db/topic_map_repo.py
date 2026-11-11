"""课题地图保留首写；标题、摘要和最新提取只做批量读取。"""

import json
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.dialects.sqlite import insert

from litgleaner.domain.language import LanguageCode
from litgleaner.domain.topic_map import TopicMap, TopicMapSettings
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.papers_repo import _to_paper
from litgleaner.infra.db.schema import extraction, paper, topic_map, triage
from litgleaner.ports.store import Extraction
from litgleaner.ports.topic_map import MapSnapshot


def _relevant(topic_id: str, topic_hash: str) -> Select[Any]:
    return select(triage.c.paper_id).where(
        triage.c.topic_id == topic_id,
        triage.c.topic_hash == topic_hash,
        triage.c.relevant.is_(True),
    )


class SqlTopicMapRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def snapshot(self, topic_id: str, topic_hash: str) -> MapSnapshot:
        relevant = _relevant(topic_id, topic_hash)
        with self._db.atomic():
            papers = tuple(
                _to_paper(row)
                for row in self._db.fetch(
                    select(paper).where(paper.c.id.in_(relevant)).order_by(paper.c.id)
                )
            )
            if not papers:
                return MapSnapshot((), {})
            latest = (
                select(func.max(extraction.c.id))
                .where(extraction.c.paper_id.in_(relevant))
                .group_by(extraction.c.paper_id)
            )
            extractions = {
                row.paper_id: Extraction(
                    row.id,
                    row.paper_id,
                    row.input_hash,
                    row.extractor_version,
                    json.loads(row.data),
                )
                for row in self._db.fetch(select(extraction).where(extraction.c.id.in_(latest)))
            }
        return MapSnapshot(papers, extractions)

    def add(self, result: TopicMap) -> TopicMap:
        result = TopicMap.model_validate(result.model_dump())
        names = ("id", "topic_id", "topic_hash", "input_hash", "producer_version", "created_at")
        if any(not getattr(result, name).strip() for name in (*names, "model", "scope_id")):
            raise ValueError("topic_map_identifier_empty")
        with self._db.atomic():
            existing = self.by_fingerprint(
                result.topic_id, result.topic_hash, result.input_hash, result.producer_version
            )
            if existing is not None:
                return existing
            self._check_papers(result.considered_paper_ids)
            self._db.write(
                insert(topic_map).values(
                    **result.model_dump(include=set(names)), data=result.model_dump_json()
                )
            )
        return result

    def get(self, map_id: str) -> TopicMap | None:
        rows = self._db.fetch(select(topic_map.c.data).where(topic_map.c.id == map_id))
        return TopicMap.model_validate_json(rows[0].data) if rows else None

    def by_fingerprint(
        self, topic_id: str, topic_hash: str, input_hash: str, producer_version: str
    ) -> TopicMap | None:
        rows = self._db.fetch(
            select(topic_map.c.data).where(
                topic_map.c.topic_id == topic_id,
                topic_map.c.topic_hash == topic_hash,
                topic_map.c.input_hash == input_hash,
                topic_map.c.producer_version == producer_version,
            )
        )
        return TopicMap.model_validate_json(rows[0].data) if rows else None

    def latest(self, topic_id: str) -> TopicMap | None:
        history = self.history(topic_id, limit=1)
        return history[0] if history else None

    def history(self, topic_id: str, limit: int = 20) -> tuple[TopicMap, ...]:
        if type(limit) is not int or limit < 1:
            raise ValueError("topic_map_limit_invalid")
        rows = self._db.fetch(
            select(topic_map.c.data)
            .where(topic_map.c.topic_id == topic_id)
            .order_by(topic_map.c.created_at.desc(), topic_map.c.id.desc())
            .limit(limit)
        )
        return tuple(TopicMap.model_validate_json(row.data) for row in rows)

    def compatible(
        self,
        topic_id: str,
        topic_hash: str,
        producer_version: str,
        reading_language: LanguageCode,
        token_budget: int,
    ) -> tuple[TopicMap, ...]:
        """不按列表页限额截断；较旧的结果仍可能匹配当前已发送事实。"""
        TopicMapSettings(token_budget=token_budget)
        if reading_language not in ("zh-CN", "en"):
            raise ValueError("topic_map_language_invalid")
        rows = self._db.fetch(
            select(topic_map.c.data)
            .where(
                topic_map.c.topic_id == topic_id,
                topic_map.c.topic_hash == topic_hash,
                topic_map.c.producer_version == producer_version,
                func.json_extract(topic_map.c.data, "$.reading_language") == reading_language,
                func.json_extract(topic_map.c.data, "$.token_budget") == token_budget,
            )
            .order_by(topic_map.c.created_at.desc(), topic_map.c.id.desc())
        )
        return tuple(TopicMap.model_validate_json(row.data) for row in rows)

    def _check_papers(self, paper_ids: tuple[str, ...]) -> None:
        rows = self._db.fetch(
            text(
                "SELECT item.value FROM json_each(:ids) item LEFT JOIN paper p ON p.id=item.value "
                "WHERE p.id IS NULL LIMIT 1"
            ).bindparams(ids=json.dumps(paper_ids))
        )
        if rows:
            raise ValueError("topic_map_paper_missing")
