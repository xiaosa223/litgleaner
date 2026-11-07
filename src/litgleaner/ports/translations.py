"""段落译文缓存的接口（第 5 节 translation 表）。"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TranslationKey:
    unit: str  # block：段落译文
    source_hash: str
    target_lang: str
    glossary_hash: str
    translator_version: str  # 提示词版本 + 模型


@dataclass(frozen=True)
class CachedTranslation:
    key: TranslationKey
    sentences: tuple[str, ...]
    model: str
    created_at: str


class TranslationRepo(Protocol):
    def get(self, key: TranslationKey) -> CachedTranslation | None: ...
    def latest_for(
        self, unit: str, source_hashes: Iterable[str], target_lang: str
    ) -> tuple[CachedTranslation, ...]: ...
    def put(
        self,
        key: TranslationKey,
        sentences: tuple[str, ...],
        model: str,
        paper_id: str | None,
        now: str,
    ) -> None: ...
