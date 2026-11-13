"""新建研究画像和课题列表（第 7.1、12.8 节）。只写定义，不启用、不调用模型。"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from litgleaner.application.context import AppContext
from litgleaner.domain.errors import ConfigError, InvalidRequest
from litgleaner.domain.topic import Topic, TopicGoal
from litgleaner.ports.topic_settings import TopicFields

DEFAULT_RUBRIC = "default"
NEW = "new"  # 有定义文件，还没有启用过
MAX_NAME = 100
MAX_GOALS = 20
MAX_ID = 40
# Windows 不允许这些文件名，带扩展名也不行
_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{n}" for p in ("com", "lpt") for n in range(10)}


@dataclass(frozen=True)
class TopicSummary:
    id: str
    name: str
    goals: tuple[TopicGoal, ...]
    status: str  # active、paused 或 new
    terms: int  # 检索词个数（含同义词）；0 表示还没有起草检索式


def topic_id_for(name: str, taken: Collection[str]) -> str:
    """纯英文名转成文件名；含其他文字时用 topic，避免只取英文片段误导。"""
    words = re.findall(r"[a-z0-9]+", name.lower()) if name.isascii() else []
    base = "-".join(words)[:MAX_ID].strip("-") or "topic"
    if base in _RESERVED:
        base = f"topic-{base}"
    candidate, number = base, 2
    while candidate in taken:
        candidate, number = f"{base}-{number}", number + 1
    return candidate


def _rubric(rubrics: Collection[str]) -> str:
    if DEFAULT_RUBRIC in rubrics:
        return DEFAULT_RUBRIC
    if not rubrics:
        raise ConfigError("没有可用的评价指标（config/rubrics/）")
    return sorted(rubrics)[0]


def define_topic(
    fields: TopicFields, taken: Collection[str], rubrics: Mapping[str, object]
) -> Topic:
    name, background, content = (
        fields.name.strip(),
        fields.background.strip(),
        fields.content.strip(),
    )
    goals = [goal.strip() for goal in fields.goals if goal.strip()]
    if not (name and background and content and goals):
        raise InvalidRequest("topic_incomplete")
    if len(name) > MAX_NAME or len(goals) > MAX_GOALS:
        raise InvalidRequest("topic_too_large")
    try:
        return Topic(
            id=topic_id_for(name, taken),
            name=name,
            background=background,
            content=content,
            goals=tuple(TopicGoal(id=f"g{i}", text=text) for i, text in enumerate(goals, 1)),
            rubric=_rubric(list(rubrics)),
        )
    except ValidationError:
        raise InvalidRequest("topic_invalid") from None


def topic_summaries(ctx: AppContext) -> tuple[TopicSummary, ...]:
    states = {state.topic_id: state.status for state in ctx.store.judgments.topic_states()}
    return tuple(
        TopicSummary(t.id, t.name, t.goals, states.get(t.id, NEW), len(t.query.terms))
        for t in ctx.topics.values()
    )
