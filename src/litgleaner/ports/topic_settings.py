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


@dataclass(frozen=True)
class GoalText:
    """id 为 None 表示新增的目标；已有目标保留原编号，评估里的目标关联才不会错位。"""

    id: str | None
    text: str


@dataclass(frozen=True)
class TopicProfile:
    """修改已有课题的研究画像；检索式、术语表、评价指标不在这里改。"""

    name: str
    background: str
    content: str
    goals: tuple[GoalText, ...]


@dataclass(frozen=True)
class ProfileChange:
    topic: Topic
    changed: bool  # 背景、内容或目标变了：之后的新论文按新定义初筛，历史论文要回填


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

    def update_profile(self, topic_id: str, profile: TopicProfile) -> ProfileChange:
        """写入修改后的名称、背景、内容和目标；启用中的课题之后的新论文按新定义处理。"""
        ...

    def set_tracking(self, topic_id: str, *, active: bool) -> None:
        """启用或暂停；没有检索词时不能启用。与修改定义互斥。"""
        ...
