"""新建研究画像和课题列表（第 7.1、12.8 节）。只写定义，不启用、不调用模型。"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from itertools import count

from pydantic import ValidationError

from litgleaner.application.context import AppContext
from litgleaner.domain.errors import ConfigError, InvalidRequest
from litgleaner.domain.topic import Topic, TopicGoal
from litgleaner.ports.topic_settings import GoalText, TopicFields, TopicProfile

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


def _texts(name: str, background: str, content: str, goals: int) -> tuple[str, str, str]:
    stripped = (name.strip(), background.strip(), content.strip())
    if not all(stripped) or goals == 0:
        raise InvalidRequest("topic_incomplete")
    if len(stripped[0]) > MAX_NAME or goals > MAX_GOALS:
        raise InvalidRequest("topic_too_large")
    return stripped


def define_topic(
    fields: TopicFields, taken: Collection[str], rubrics: Mapping[str, object]
) -> Topic:
    goals = [goal.strip() for goal in fields.goals if goal.strip()]
    name, background, content = _texts(fields.name, fields.background, fields.content, len(goals))
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


def _goal_ids(before: Topic, goals: list[GoalText]) -> list[str]:
    """已有目标保留编号，新目标接着现有最大的编号往后排。

    以前删掉的最大编号可能被再次用到；这不会把旧判断挂到新目标上：评估、阅读路线和综述提纲都按
    topic_hash 区分，改目标后旧结果不再显示。
    """
    known = {goal.id for goal in before.goals}
    kept = [goal.id for goal in goals if goal.id is not None]
    if any(goal_id not in known for goal_id in kept) or len(set(kept)) != len(kept):
        raise InvalidRequest("topic_goals_invalid")
    numbers = [int(i[1:]) for i in known if re.fullmatch(r"g\d+", i)]
    following = count(max(numbers, default=0) + 1)
    return [goal.id if goal.id is not None else f"g{next(following)}" for goal in goals]


def revise_topic(before: Topic, profile: TopicProfile) -> Topic:
    """只改研究者自己写的部分；编号、检索式、术语表、评价指标不变。"""
    goals = [GoalText(goal.id, goal.text.strip()) for goal in profile.goals if goal.text.strip()]
    name, background, content = _texts(
        profile.name, profile.background, profile.content, len(goals)
    )
    ids = _goal_ids(before, goals)
    try:
        return Topic.model_validate(
            {
                **before.model_dump(),
                "name": name,
                "background": background,
                "content": content,
                "goals": [{"id": i, "text": goal.text} for i, goal in zip(ids, goals, strict=True)],
            }
        )
    except ValidationError:
        raise InvalidRequest("topic_invalid") from None


def topic_summaries(ctx: AppContext) -> tuple[TopicSummary, ...]:
    states = {state.topic_id: state.status for state in ctx.store.judgments.topic_states()}
    return tuple(
        TopicSummary(t.id, t.name, t.goals, states.get(t.id, NEW), len(t.query.terms))
        for t in ctx.topics.values()
    )
