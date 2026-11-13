"""在领读器里新建研究画像、确认检索式（第 7.1、7.2 节）：写主题文件，并替换后续请求的运行时。"""

from dataclasses import dataclass
from typing import Protocol

from litgleaner.domain.glossary import GlossaryTerm
from litgleaner.domain.topic import Topic, TopicQuery


@dataclass(frozen=True)
class TopicFields:
    """研究者自己写的部分；检索式由 topic draft 起草后再确认写入。"""

    name: str
    background: str
    content: str
    goals: tuple[str, ...]


class TopicSettings(Protocol):
    def create(self, fields: TopicFields) -> Topic:
        """写入新的主题文件，不启用、不调用模型；之后的请求立即能看到它。"""
        ...

    def apply_query(
        self, topic_id: str, query: TopicQuery, glossary: tuple[GlossaryTerm, ...]
    ) -> Topic:
        """把研究者确认的检索式和术语表写入文件；从未启用过的课题随之启用，暂停的保持暂停。

        文件被手改过时拒绝覆盖。
        """
        ...
