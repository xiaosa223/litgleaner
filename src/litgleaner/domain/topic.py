"""研究主题：定义在配置文件，是否启用记录在数据库。"""

from collections.abc import Mapping
from typing import Any

from pydantic import Field, field_validator, model_validator

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


class TopicGoal(FrozenModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1)


class Topic(FrozenModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str
    background: str = Field(min_length=1)
    content: str = Field(min_length=1)
    goals: tuple[TopicGoal, ...] = Field(min_length=1)
    query: TopicQuery = TopicQuery()
    journal_sets: tuple[str, ...] = ()
    seeds: TopicSeeds = TopicSeeds()
    rubric: str
    public: bool = False

    @model_validator(mode="before")
    @classmethod
    def _reject_legacy_description(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "description" in data:
            raise ValueError(
                "description 字段已拆分：请改写为 background（研究背景）、"
                "content（研究内容）和 goals（研究目标列表，每项含 id 和 text）"
            )
        return data

    @field_validator("goals")
    @classmethod
    def _unique_goal_ids(cls, goals: tuple[TopicGoal, ...]) -> tuple[TopicGoal, ...]:
        ids = [goal.id for goal in goals]
        if duplicated := sorted({goal_id for goal_id in ids if ids.count(goal_id) > 1}):
            raise ValueError(f"研究目标 id 重复：{duplicated}")
        return goals

    def profile_text(self) -> str:
        """给模型看的研究画像。"""
        goals = "\n".join(f"- {goal.id}：{goal.text}" for goal in self.goals)
        return (
            f"研究背景：{self.background.strip()}\n"
            f"研究内容：{self.content.strip()}\n"
            f"研究目标：\n{goals}"
        )

    @property
    def topic_hash(self) -> str:
        """只覆盖影响判断的字段。改名字或公开设置不会让初筛结果失效。"""
        return stable_hash(
            {
                "background": self.background,
                "content": self.content,
                "goals": [goal.model_dump() for goal in self.goals],
                "include": self.query.include,
                "synonyms": dict(self.query.synonyms),
                "exclude": self.query.exclude,
            }
        )
