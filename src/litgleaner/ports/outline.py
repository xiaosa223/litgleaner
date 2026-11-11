"""综述骨架结果首写保存；历史读取不触发生成或事实补齐。"""

from typing import Protocol

from litgleaner.domain.language import LanguageCode
from litgleaner.domain.outline import TopicOutline


class OutlineRepo(Protocol):
    def add(self, result: TopicOutline) -> TopicOutline: ...
    def get(self, outline_id: str) -> TopicOutline | None: ...
    def by_fingerprint(
        self, topic_id: str, topic_hash: str, input_hash: str, producer_version: str
    ) -> TopicOutline | None: ...
    def latest(self, topic_id: str) -> TopicOutline | None: ...
    def history(self, topic_id: str, limit: int = 20) -> tuple[TopicOutline, ...]: ...
    def compatible(
        self,
        topic_id: str,
        topic_hash: str,
        producer_version: str,
        reading_language: LanguageCode,
        token_budget: int,
    ) -> tuple[TopicOutline, ...]: ...
