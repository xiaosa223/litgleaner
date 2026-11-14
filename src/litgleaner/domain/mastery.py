"""掌握程度与复习间隔：从事件按时间顺序算出，纯函数，规则变了零费用重算（第 12.7 节）。

规则（ADR 0007）：
- 读过含该概念的论文只能到“见过”；只有经过评价的用户回答、复习或用户修改能更高。
- 回答评价只升不降；用户修改直接定级，之后的评价和复习仍可在此基础上提升。
- 到“能解释”后进入复习：记得进一档，模糊停在原档，忘了回到第一档；忘了不降级。
- 在“能解释”上累计记得达到次数后升到“能应用”。
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from litgleaner.domain.hashing import stable_hash

MasteryLevel = Literal[0, 1, 2, 3]
EventKind = Literal["evaluation", "review", "override"]
Rating = Literal["remembered", "fuzzy", "forgot"]
QuestionKind = Literal["understand", "compare", "transfer"]
Verdict = Literal["correct", "missing", "misunderstood"]
Overall = Literal["correct", "partial", "incorrect"]

UNSEEN, SEEN, EXPLAIN, APPLY = 0, 1, 2, 3
POLICY_VERSION = "mastery-v2"


@dataclass(frozen=True)
class MasteryEvent:
    id: str
    concept_id: str
    kind: EventKind
    level: int | None  # 只有 override 有：用户定的等级
    rating: Rating | None  # 只有 review 有
    source_id: str | None  # 评价的编号
    created_at: str
    # evaluation 只记评价事实，等级由规则在计算时得出，规则变了零费用重算
    question_kind: QuestionKind | None = None
    verdict: Verdict | None = None
    overall: Overall | None = None


@dataclass(frozen=True)
class MasteryPolicy:
    intervals: tuple[int, ...] = (1, 3, 7, 14, 30)
    remembers_to_apply: int = 3

    @property
    def policy_hash(self) -> str:
        return stable_hash(
            {
                "version": POLICY_VERSION,
                "intervals": list(self.intervals),
                "remembers_to_apply": self.remembers_to_apply,
            }
        )


DEFAULT_POLICY = MasteryPolicy()


@dataclass(frozen=True)
class MasteryState:
    level: int
    overridden: bool
    due_at: str | None  # None 表示不在复习计划里
    step: int
    remembered: int
    reviews: int
    policy_hash: str


def evaluation_level(kind: QuestionKind, verdict: Verdict, overall: Overall) -> int:
    """回答评价能证明的等级：概念讲对到“能解释”；对比、迁移题整题答对才到“能应用”。"""
    if verdict != "correct":
        return SEEN
    return APPLY if kind != "understand" and overall == "correct" else EXPLAIN


def _evaluated(event: MasteryEvent) -> int:
    if event.question_kind is None or event.verdict is None or event.overall is None:
        return UNSEEN
    return evaluation_level(event.question_kind, event.verdict, event.overall)


def _later(at: str, days: int) -> str:
    return (datetime.fromisoformat(at) + timedelta(days=days)).isoformat()


def _sorted(events: Iterable[MasteryEvent]) -> list[MasteryEvent]:
    return sorted(events, key=lambda e: (datetime.fromisoformat(e.created_at), e.id))


def _clamp(level: int | None) -> int:
    return max(UNSEEN, min(APPLY, level if level is not None else UNSEEN))


def compute_mastery(
    events: Iterable[MasteryEvent], *, seen: bool, policy: MasteryPolicy = DEFAULT_POLICY
) -> MasteryState:
    intervals = policy.intervals or (1,)
    level = SEEN if seen else UNSEEN
    overridden = False
    step = remembered = reviews = 0
    due: str | None = None
    for event in _sorted(events):
        before = level
        if event.kind == "override":
            level, overridden = _clamp(event.level), True
        elif event.kind == "evaluation":
            level = max(level, _evaluated(event))
        elif event.kind == "review" and level >= EXPLAIN:
            reviews += 1
            if event.rating == "remembered":
                step, remembered = step + 1, remembered + 1
                if level == EXPLAIN and remembered >= policy.remembers_to_apply:
                    level = APPLY
            elif event.rating == "forgot":
                step = 0
            due = _later(event.created_at, intervals[min(step, len(intervals) - 1)])
            continue
        if level < EXPLAIN:
            due, step, remembered = None, 0, 0
        elif before < EXPLAIN or due is None:
            due = _later(event.created_at, intervals[0])
    return MasteryState(level, overridden, due, step, remembered, reviews, policy.policy_hash)


def is_due(state: MasteryState, now: str) -> bool:
    return state.due_at is not None and datetime.fromisoformat(
        state.due_at
    ) <= datetime.fromisoformat(now)
