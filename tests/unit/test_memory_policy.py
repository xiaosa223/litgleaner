"""候选只加权已有信号，配置与算法共同决定策略指纹。"""

import math
from dataclasses import FrozenInstanceError, replace

import pytest
from pydantic import ValidationError

from litgleaner.domain import memory_policy
from litgleaner.domain.memory_policy import CandidateSignals, MemoryPolicy, MemoryWeights, score


def test_default_policy_and_empty_signal_are_explicit_and_immutable():
    policy = MemoryPolicy()
    assert policy.max_pairs == 5 and policy.common_reference_min == 2
    assert policy.cosine_min == policy.minimum_score == 0.5 and policy.query_terms_limit == 32
    assert tuple(policy.weights.model_dump().values()) == (6, 5, 4, 4, 4, 3, 2, 1)
    result = score(CandidateSignals(), policy)
    assert result.total == 0 and len(result.components) == 8
    assert all(item.value == item.contribution == 0 for item in result.components)
    with pytest.raises(ValidationError):
        policy.max_pairs = 6
    with pytest.raises(FrozenInstanceError):
        result.total = 3


def test_contributions_are_stable_saturated_and_auditable():
    signals = CandidateSignals(True, True, 2, 3, 4, 5, 0.75, 0.2)
    result = score(signals, MemoryPolicy())
    assert tuple(item.name for item in result.components) == (
        "direct_citation",
        "citation_intent",
        "common_references",
        "core_concepts",
        "methods",
        "cosine",
        "datasets",
        "text",
    )
    assert tuple(item.value for item in result.components) == (1, 1, 1, 1, 1, 0.75, 1, 0.2)
    assert tuple(item.weight for item in result.components) == (6, 5, 4, 4, 4, 3, 2, 1)
    assert result.total == pytest.approx(27.45)
    assert result.total == math.fsum(item.contribution for item in result.components)
    repeated = replace(signals, common_references=200, core_concepts=300, methods=400, datasets=500)
    assert score(repeated, MemoryPolicy()) == result


def test_common_reference_and_cosine_thresholds_are_inclusive():
    policy = MemoryPolicy()
    assert score(CandidateSignals(common_references=1, cosine=0.499), policy).total == 0
    assert score(CandidateSignals(common_references=2, cosine=0.5), policy).total == 5.5
    assert score(CandidateSignals(cosine=-1), policy).total == 0
    assert score(CandidateSignals(cosine=-0.5), MemoryPolicy(cosine_min=0)).total == 0


def test_reweighting_preserves_signals_and_does_not_apply_limit_or_total_threshold():
    signals = CandidateSignals(direct_citation=True, datasets=1, text=1)
    policy = MemoryPolicy(
        weights=MemoryWeights(direct_citation=0, datasets=10, text=2),
        max_pairs=1,
        minimum_score=100,
    )
    result = score(signals, policy)
    assert result.total == 12
    assert all(type(item.value) is float for item in result.components)
    assert signals == CandidateSignals(direct_citation=True, datasets=1, text=1)
    zero = MemoryWeights(**dict.fromkeys(MemoryWeights.model_fields, 0))
    assert score(signals, MemoryPolicy(weights=zero)).total == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_pairs", 6),
        ("common_reference_min", 3),
        ("cosine_min", 0.75),
        ("minimum_score", 1),
        ("query_terms_limit", 12),
    ],
)
def test_every_policy_option_affects_hash(field, value):
    original = MemoryPolicy()
    assert original.policy_hash != MemoryPolicy(**{field: value}).policy_hash
    assert original.policy_hash == MemoryPolicy.model_validate(original.model_dump()).policy_hash


def test_weight_and_protocol_changes_affect_hash(monkeypatch):
    policy = MemoryPolicy()
    original = policy.policy_hash
    assert original != MemoryPolicy(weights=MemoryWeights(text=2)).policy_hash
    monkeypatch.setattr(memory_policy, "POLICY_VERSION", "changed-protocol")
    assert original != policy.policy_hash
    assert MemoryPolicy(weights=MemoryWeights(text=1)).policy_hash == policy.policy_hash
    changed = policy.policy_hash
    monkeypatch.setattr(memory_policy, "TERMS_VERSION", "changed-term-protocol")
    assert policy.policy_hash != changed


@pytest.mark.parametrize("field", tuple(MemoryWeights.model_fields))
@pytest.mark.parametrize("value", [-1, 1000001, True, "2", math.inf, -math.inf, math.nan])
def test_weights_reject_invalid_values(field, value):
    with pytest.raises(ValidationError):
        MemoryWeights(**{field: value})


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_pairs", 0),
        ("max_pairs", 201),
        ("max_pairs", True),
        ("max_pairs", "5"),
        ("common_reference_min", 0),
        ("common_reference_min", 1000001),
        ("common_reference_min", 1.5),
        ("common_reference_min", False),
        ("cosine_min", -0.1),
        ("cosine_min", 1.1),
        ("cosine_min", True),
        ("cosine_min", math.nan),
        ("minimum_score", -1),
        ("minimum_score", math.inf),
        ("minimum_score", 1000000001),
        ("query_terms_limit", 0),
        ("query_terms_limit", 65),
        ("query_terms_limit", False),
        ("query_terms_limit", "32"),
    ],
)
def test_policy_rejects_invalid_thresholds_and_limits(field, value):
    with pytest.raises(ValidationError):
        MemoryPolicy(**{field: value})


def test_unknown_fields_are_rejected_at_both_configuration_levels():
    with pytest.raises(ValidationError):
        MemoryWeights(unknown=1)
    with pytest.raises(ValidationError):
        MemoryPolicy(unknown=1)


@pytest.mark.parametrize("field", ["common_references", "core_concepts", "methods", "datasets"])
@pytest.mark.parametrize("value", [-1, True, 1.5, math.inf, math.nan, "1"])
def test_counts_must_be_nonnegative_integers(field, value):
    with pytest.raises(ValueError):
        score(CandidateSignals(**{field: value}), MemoryPolicy())


@pytest.mark.parametrize("field", ["direct_citation", "citation_intent"])
@pytest.mark.parametrize("value", [0, 1, "yes", None])
def test_flags_must_be_explicit_booleans(field, value):
    with pytest.raises(ValueError):
        score(CandidateSignals(**{field: value}), MemoryPolicy())


@pytest.mark.parametrize(
    "field,value",
    [
        ("cosine", -1.1),
        ("cosine", 1.1),
        ("text", -0.1),
        ("text", 1.1),
        ("cosine", True),
        ("text", False),
        ("cosine", math.nan),
        ("text", math.inf),
        ("cosine", -math.inf),
        ("text", "0.5"),
        ("cosine", 10**1000),
        ("text", -(10**1000)),
    ],
)
def test_continuous_signals_reject_invalid_values(field, value):
    with pytest.raises(ValueError):
        score(CandidateSignals(**{field: value}), MemoryPolicy())
