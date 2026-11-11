"""课题地图批量读取事实，生成结果保留首次写入。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from litgleaner.domain.language import LanguageCode
from litgleaner.domain.paper import Paper
from litgleaner.domain.topic_map import TopicMap

if TYPE_CHECKING:
    from litgleaner.ports.store import Extraction


@dataclass(frozen=True)
class MapSnapshot:
    papers: tuple[Paper, ...]
    extractions: Mapping[str, Extraction]


class TopicMapRepo(Protocol):
    def snapshot(self, topic_id: str, topic_hash: str) -> MapSnapshot: ...
    def add(self, result: TopicMap) -> TopicMap: ...
    def get(self, map_id: str) -> TopicMap | None: ...
    def by_fingerprint(
        self, topic_id: str, topic_hash: str, input_hash: str, producer_version: str
    ) -> TopicMap | None: ...
    def latest(self, topic_id: str) -> TopicMap | None: ...
    def history(self, topic_id: str, limit: int = 20) -> tuple[TopicMap, ...]: ...
    def compatible(
        self,
        topic_id: str,
        topic_hash: str,
        producer_version: str,
        reading_language: LanguageCode,
        token_budget: int,
    ) -> tuple[TopicMap, ...]: ...
