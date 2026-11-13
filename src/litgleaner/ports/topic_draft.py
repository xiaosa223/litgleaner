"""检索式草稿的存取（第 7.2 节）：写在主题文件旁边，不会被当成主题定义加载。"""

from typing import Protocol

from litgleaner.domain.topic_draft import TopicDraft


class TopicDraftStore(Protocol):
    def save(self, draft: TopicDraft) -> None: ...
    def load(self, topic_id: str) -> TopicDraft | None:
        """没有草稿或草稿文件已损坏时返回 None。"""
        ...
