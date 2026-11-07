"""研究主题：定义在配置文件，是否启用记录在数据库。"""

from collections.abc import Mapping

from pydantic import Field

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.hashing import stable_hash


class TopicQuery(FrozenModel):
    include: tuple[str, ...] = ()
    synonyms: Mapping[str, tuple[str, ...]] = Field(default_factory=dict)
    exclude: tuple[str, ...] = ()

    @property
    def terms(self) -> tuple[str, ...]:
        expanded = [*self.include]
        for term in self.include:
            expanded.extend(self.synonyms.get(term, ()))
        return tuple(dict.fromkeys(expanded))


class TopicSeeds(FrozenModel):
    dois: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()


class Topic(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str
    description: str = Field(min_length=1)
    query: TopicQuery = TopicQuery()
    journal_sets: tuple[str, ...] = ()
    seeds: TopicSeeds = TopicSeeds()
    rubric: str
    public: bool = False

    @property
    def topic_hash(self) -> str:
        """只覆盖影响判断的字段。改名字或公开设置不会让初筛结果失效。"""
        return stable_hash(
            {
                "description": self.description,
                "include": self.query.include,
                "synonyms": dict(self.query.synonyms),
                "exclude": self.query.exclude,
            }
        )
