"""论文候选的可配置纯评分，只排序信号，不断言语义关系。"""

import math
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from litgleaner.domain.base import FrozenModel
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.memory_terms import TERMS_VERSION

POLICY_VERSION = "memory-candidates-v1"
Weight = Annotated[float, Field(ge=0, le=1_000_000, strict=True, allow_inf_nan=False)]


class MemoryWeights(FrozenModel):
    direct_citation: Weight = 6.0
    citation_intent: Weight = 5.0
    common_references: Weight = 4.0
    core_concepts: Weight = 4.0
    methods: Weight = 4.0
    cosine: Weight = 3.0
    datasets: Weight = 2.0
    text: Weight = 1.0


class MemoryPolicy(FrozenModel):
    weights: MemoryWeights = Field(default_factory=MemoryWeights)
    max_pairs: int = Field(default=5, ge=1, le=200, strict=True)
    common_reference_min: int = Field(default=2, ge=1, le=1_000_000, strict=True)
    cosine_min: float = Field(default=0.5, ge=0, le=1, strict=True, allow_inf_nan=False)
    minimum_score: float = Field(
        default=0.5, ge=0, le=1_000_000_000, strict=True, allow_inf_nan=False
    )
    query_terms_limit: int = Field(default=32, ge=1, le=64, strict=True)

    @property
    def policy_hash(self) -> str:
        return stable_hash(
            {
                "protocol": POLICY_VERSION,
                "terms_protocol": TERMS_VERSION,
                "policy": self.model_dump(mode="json"),
            }
        )


@dataclass(frozen=True)
class CandidateSignals:
    direct_citation: bool = False
    citation_intent: bool = False
    common_references: int = 0
    core_concepts: int = 0
    methods: int = 0
    datasets: int = 0
    cosine: float | None = None
    text: float | None = None


@dataclass(frozen=True)
class ScoreComponent:
    name: str
    value: float
    weight: float
    contribution: float


@dataclass(frozen=True)
class CandidateScore:
    total: float
    components: tuple[ScoreComponent, ...]


def _number(value: float | None, minimum: float) -> None:
    if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not minimum <= value <= 1
        or not math.isfinite(value)
    ):
        raise ValueError("候选连续信号必须是范围内的有限数值")


def _validate(signals: CandidateSignals) -> None:
    if type(signals.direct_citation) is not bool or type(signals.citation_intent) is not bool:
        raise ValueError("候选引用信号必须是布尔值")
    counts = (signals.common_references, signals.core_concepts, signals.methods, signals.datasets)
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("候选事实数量必须是非负整数")
    _number(signals.cosine, -1)
    _number(signals.text, 0)


def score(signals: CandidateSignals, policy: MemoryPolicy) -> CandidateScore:
    _validate(signals)
    cosine = float(signals.cosine) if signals.cosine is not None else None
    values = {
        "direct_citation": float(signals.direct_citation),
        "citation_intent": float(signals.citation_intent),
        "common_references": float(signals.common_references >= policy.common_reference_min),
        "core_concepts": float(signals.core_concepts > 0),
        "methods": float(signals.methods > 0),
        "cosine": max(0.0, cosine) if cosine is not None and cosine >= policy.cosine_min else 0.0,
        "datasets": float(signals.datasets > 0),
        "text": float(signals.text) if signals.text is not None else 0.0,
    }
    components = tuple(
        ScoreComponent(name, value, weight, value * weight)
        for name, value in values.items()
        for weight in (getattr(policy.weights, name),)
    )
    return CandidateScore(math.fsum(item.contribution for item in components), components)
