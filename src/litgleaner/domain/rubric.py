"""评价指标：配置即定义，总分只由这里的纯函数计算，LLM 不参与。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field, model_validator

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.hashing import stable_hash

Levels = Mapping[str, int | None]
MAX_SCORE = 100
# 算分规则（compute_score）本身的版本。规则变化时加一：policy_hash 随之变化，
# 已有维度等级按新规则零费用重算，不需要重评。2：区分缺全文和无法判断，门槛为空不放行
SCORING_VERSION = 2


class Metric(FrozenModel):
    key: str
    label: str
    range: tuple[Literal[0], Literal[100]]


class LevelScale(FrozenModel):
    min: Literal[0]
    max: int = Field(gt=0)


class Dimension(FrozenModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    weight: int = Field(gt=0)
    requires_fulltext: bool
    question: str
    anchors: Mapping[str, str]


class Gate(FrozenModel):
    dimension: str
    min_level: int


class Tier(FrozenModel):
    key: str
    label: str
    min_score: int = Field(ge=0, le=MAX_SCORE)


class Provisional(FrozenModel):
    tier: str
    min_score: int = Field(ge=0, le=MAX_SCORE)


@dataclass(frozen=True)
class ScoreResult:
    score: int
    tier: str
    provisional: bool  # 缺全文：只读了摘要，需要全文的维度没有评
    gated: bool
    undetermined: tuple[str, ...] = ()  # 有输入但模型给不出等级的维度


class Rubric(FrozenModel):
    schema_version: Literal[1]
    id: str
    metric: Metric
    levels: LevelScale
    dimensions: tuple[Dimension, ...]
    gates: tuple[Gate, ...]
    tiers: tuple[Tier, ...]
    provisional: Provisional

    @model_validator(mode="after")
    def _validate_consistency(self) -> "Rubric":
        self._check_dimensions()
        self._check_gates()
        self._check_tiers()
        return self

    def _check_dimensions(self) -> None:
        keys = [dim.key for dim in self.dimensions]
        if not keys or len(set(keys)) != len(keys):
            raise ValueError("维度不能为空，key 不能重复")
        if sum(dim.weight for dim in self.dimensions) != MAX_SCORE:
            raise ValueError("权重总和必须为 100")
        allowed = {str(level) for level in range(self.levels.max + 1)}
        for dim in self.dimensions:
            if not set(dim.anchors) <= allowed:
                raise ValueError(f"维度 {dim.key} 的锚点超出等级范围")

    def _check_gates(self) -> None:
        keys = {dim.key for dim in self.dimensions}
        for gate in self.gates:
            if gate.dimension not in keys or not 0 <= gate.min_level <= self.levels.max:
                raise ValueError(f"门槛配置无效：{gate.dimension}")
        fulltext_only = {dim.key for dim in self.dimensions if dim.requires_fulltext}
        for gate in self.gates:
            if gate.dimension in fulltext_only:
                raise ValueError(
                    f"门槛维度 {gate.dimension} 是 requires_fulltext 维度：只读摘要时它必然为空，"
                    "所有摘要论文都会被门槛挡掉，进不了需要全文的等级"
                )

    def _check_tiers(self) -> None:
        tier_keys = [tier.key for tier in self.tiers]
        if len(set(tier_keys)) != len(tier_keys):
            raise ValueError("等级 key 不能重复")
        if not any(tier.min_score == 0 for tier in self.tiers):
            raise ValueError("必须有一个 min_score 为 0 的兜底等级")
        if self.provisional.tier in tier_keys:
            raise ValueError("provisional.tier 不能和普通等级重名")

    @property
    def semantic_hash(self) -> str:
        """决定 LLM 要回答什么。变化后旧等级失效，需要付费重评。"""
        return stable_hash(
            {
                "levels": self.levels.model_dump(),
                "dimensions": [
                    {
                        "key": dim.key,
                        "requires_fulltext": dim.requires_fulltext,
                        "question": dim.question,
                        "anchors": dict(dim.anchors),
                    }
                    for dim in self.dimensions
                ],
            }
        )

    @property
    def policy_hash(self) -> str:
        """决定等级如何换算成分数。变化后本地重算，不产生 LLM 费用。"""
        return stable_hash(
            {
                "scoring_version": SCORING_VERSION,
                "weights": {dim.key: dim.weight for dim in self.dimensions},
                "gates": [gate.model_dump() for gate in self.gates],
                "tiers": [tier.model_dump() for tier in self.tiers],
                "provisional": self.provisional.model_dump(),
            }
        )

    @property
    def fallback_tier(self) -> str:
        return min(self.tiers, key=lambda tier: tier.min_score).key

    def assessment_schema(self) -> dict[str, Any]:
        """LLM 的输出结构。只有维度等级和证据，没有总分字段。"""
        dimension = {
            "type": "object",
            "additionalProperties": False,
            "required": ["level", "reason", "quote"],
            "properties": {
                "level": {
                    "type": ["integer", "null"],
                    "minimum": self.levels.min,
                    "maximum": self.levels.max,
                },
                "reason": {"type": "string", "maxLength": 400},
                "quote": {"type": ["string", "null"], "maxLength": 800},
            },
        }
        keys = [dim.key for dim in self.dimensions]
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["dimensions"],
            "properties": {
                "dimensions": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": keys,
                    "properties": dict.fromkeys(keys, dimension),
                }
            },
        }


def _check_levels(rubric: Rubric, levels: Levels) -> None:
    keys = {dim.key for dim in rubric.dimensions}
    if missing := keys - set(levels):
        raise ValueError(f"缺少维度等级：{sorted(missing)}")
    if unknown := set(levels) - keys:
        raise ValueError(f"未知维度：{sorted(unknown)}")
    for key, level in levels.items():
        if level is not None and not rubric.levels.min <= level <= rubric.levels.max:
            raise ValueError(f"维度 {key} 的等级 {level} 超出范围")


def _weighted_score(rubric: Rubric, levels: Levels) -> int:
    """整数运算避免浮点误差，并且四舍五入（内置 round 是银行家舍入）。"""
    known = [
        (dim.weight, levels[dim.key]) for dim in rubric.dimensions if levels[dim.key] is not None
    ]
    denominator = sum(weight for weight, _ in known) * rubric.levels.max
    if denominator == 0:
        return 0
    numerator = sum(weight * (level or 0) for weight, level in known) * MAX_SCORE
    return (2 * numerator + denominator) // (2 * denominator)


def _tier_for(rubric: Rubric, score: int) -> str:
    eligible = [tier for tier in rubric.tiers if score >= tier.min_score]
    return max(eligible, key=lambda tier: tier.min_score).key


def _undetermined(rubric: Rubric, levels: Levels, fulltext_used: bool) -> tuple[str, ...]:
    """等级为空、且不是因为缺全文的维度。"""
    return tuple(
        dim.key
        for dim in rubric.dimensions
        if levels[dim.key] is None and (fulltext_used or not dim.requires_fulltext)
    )


def compute_score(rubric: Rubric, levels: Levels, *, fulltext_used: bool) -> ScoreResult:
    """维度为空有两种原因：缺全文（临时分，不能必读）和无法判断（不限制等级，只做标记）。"""
    _check_levels(rubric, levels)
    score = _weighted_score(rubric, levels)
    provisional = not fulltext_used and any(dim.requires_fulltext for dim in rubric.dimensions)
    # 门槛维度为空同样不放行，交给误杀抽查
    gated = any(
        (level := levels[gate.dimension]) is None or level < gate.min_level for gate in rubric.gates
    )
    if gated:
        tier = rubric.fallback_tier
    elif provisional:
        passes = score >= rubric.provisional.min_score
        tier = rubric.provisional.tier if passes else rubric.fallback_tier
    else:
        tier = _tier_for(rubric, score)
    return ScoreResult(
        score=score,
        tier=tier,
        provisional=provisional,
        gated=gated,
        undetermined=_undetermined(rubric, levels, fulltext_used),
    )


def apply_fulltext_constraint(
    rubric: Rubric, levels: Levels, fulltext_used: bool
) -> dict[str, int | None]:
    """没读全文就无法判断的维度，无论模型怎么回答都置空。"""
    if fulltext_used:
        return dict(levels)
    needs_fulltext = {dim.key for dim in rubric.dimensions if dim.requires_fulltext}
    return {key: None if key in needs_fulltext else level for key, level in levels.items()}
