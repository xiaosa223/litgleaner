"""段落译文缓存。键与 translation 表主键一致，同一键重复写入以第一次为准。"""

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import Row, select
from sqlalchemy.dialects.sqlite import insert

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.schema import translation
from litgleaner.ports.translations import CachedTranslation, TranslationKey


def _to_cached(row: Row[Any]) -> CachedTranslation:
    key = TranslationKey(
        row.unit, row.source_hash, row.target_lang, row.glossary_hash, row.translator_version
    )
    return CachedTranslation(key, tuple(json.loads(row.sentences)), row.model, row.created_at)


class SqlTranslationRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: TranslationKey) -> CachedTranslation | None:
        rows = self._db.fetch(
            select(translation).where(
                translation.c.unit == key.unit,
                translation.c.source_hash == key.source_hash,
                translation.c.target_lang == key.target_lang,
                translation.c.glossary_hash == key.glossary_hash,
                translation.c.translator_version == key.translator_version,
            )
        )
        return _to_cached(rows[0]) if rows else None

    def latest_for(
        self, unit: str, source_hashes: Iterable[str], target_lang: str
    ) -> tuple[CachedTranslation, ...]:
        """这些原文已有的所有译文，新的在前。"""
        hashes = list(set(source_hashes))
        if not hashes:
            return ()
        rows = self._db.fetch(
            select(translation)
            .where(
                translation.c.unit == unit,
                translation.c.target_lang == target_lang,
                translation.c.source_hash.in_(hashes),
            )
            .order_by(translation.c.created_at.desc())
        )
        return tuple(_to_cached(row) for row in rows)

    def put(
        self,
        key: TranslationKey,
        sentences: tuple[str, ...],
        model: str,
        paper_id: str | None,
        now: str,
    ) -> None:
        self._db.write(
            insert(translation)
            .values(
                unit=key.unit,
                source_hash=key.source_hash,
                target_lang=key.target_lang,
                glossary_hash=key.glossary_hash,
                translator_version=key.translator_version,
                sentences=json.dumps(list(sentences), ensure_ascii=False),
                model=model,
                paper_id=paper_id,
                created_at=now,
            )
            .on_conflict_do_nothing()
        )
