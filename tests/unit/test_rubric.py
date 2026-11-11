"""评价指标：唯一分数由代码计算，修改配置时影响范围可判断。"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import litgleaner.domain.rubric as module
from litgleaner.domain.rubric import Rubric, apply_fulltext_constraint, compute_score

DEFAULT = Path(__file__).parents[2] / "config" / "rubrics" / "default.json"


def raw() -> dict:
    return json.loads(DEFAULT.read_text(encoding="utf-8"))


def rubric() -> Rubric:
    return Rubric.model_validate(raw())


def levels(rel, nov, evi, reu) -> dict:
    return {"relevance": rel, "novelty": nov, "evidence": evi, "reusability": reu}


@pytest.mark.parametrize(
    ("given", "score", "tier"),
    [
        (levels(5, 5, 5, 5), 100, "must_read"),
        (levels(4, 4, 3, 3), 72, "digest"),
        (levels(4, 4, 4, 4), 80, "must_read"),
        (levels(3, 3, 3, 3), 60, "digest"),
        (levels(2, 2, 2, 2), 40, "drop"),
    ],
)
def test_score_and_tier(given, score, tier):
    result = compute_score(rubric(), given, fulltext_used=True)
    assert (result.score, result.tier, result.provisional, result.gated) == (
        score,
        tier,
        False,
        False,
    )
    assert result.undetermined == ()


def test_gate_failure_drops_even_with_high_other_levels():
    result = compute_score(rubric(), levels(1, 5, 5, 5), fulltext_used=True)
    assert result.gated
    assert result.tier == "drop"


def test_missing_fulltext_is_provisional_and_cannot_be_must_read():
    result = compute_score(rubric(), levels(5, 5, None, None), fulltext_used=False)
    assert result.score == 100
    assert result.provisional
    assert result.undetermined == ()
    assert result.tier == "need_fulltext"


def test_provisional_below_threshold_is_dropped():
    result = compute_score(rubric(), levels(2, 2, None, None), fulltext_used=False)
    assert (result.score, result.tier) == (40, "drop")


def test_abstract_only_score_uses_available_weights():
    result = compute_score(rubric(), levels(4, 3, None, None), fulltext_used=False)
    assert result.score == 72


def test_undetermined_with_fulltext_is_not_provisional_and_keeps_tier():
    """有全文但模型给不出等级：只按可判断维度算分，不限制等级，记下哪些维度无法判断。"""
    result = compute_score(rubric(), levels(5, 5, None, 5), fulltext_used=True)
    assert not result.provisional
    assert result.undetermined == ("evidence",)
    assert (result.score, result.tier) == (100, "must_read")


def test_undetermined_and_missing_fulltext_are_reported_separately():
    result = compute_score(rubric(), levels(5, None, None, None), fulltext_used=False)
    assert result.provisional
    assert result.undetermined == ("novelty",)


def test_empty_gate_dimension_does_not_pass():
    for fulltext_used in (True, False):
        result = compute_score(rubric(), levels(None, 5, 5, 5), fulltext_used=fulltext_used)
        assert result.gated
        assert result.tier == "drop"
        assert "relevance" in result.undetermined


def test_abstract_only_without_fulltext_dimensions_is_not_provisional():
    data = raw()
    for dim in data["dimensions"]:
        dim["requires_fulltext"] = False
    result = compute_score(Rubric.model_validate(data), levels(5, 5, 5, 5), fulltext_used=False)
    assert not result.provisional
    assert result.tier == "must_read"


def test_all_null_is_dropped():
    result = compute_score(rubric(), levels(None, None, None, None), fulltext_used=True)
    assert (result.score, result.tier) == (0, "drop")


def test_rounds_half_up_not_bankers():
    data = raw()
    data["levels"]["max"] = 4
    weights = iter([50, 50, 0, 0])
    data["dimensions"] = [d for d in data["dimensions"] if d["key"] in {"relevance", "novelty"}]
    for dim in data["dimensions"]:
        dim["weight"] = next(weights)
        dim["anchors"] = {"0": "low"}
    custom = Rubric.model_validate(data)
    assert compute_score(custom, {"relevance": 2, "novelty": 0}, fulltext_used=True).score == 25
    data["levels"]["max"] = 8
    assert (
        compute_score(
            Rubric.model_validate(data), {"relevance": 2, "novelty": 0}, fulltext_used=True
        ).score
        == 13
    )


def test_rejects_missing_or_out_of_range_levels():
    with pytest.raises(ValueError, match="缺少"):
        compute_score(rubric(), {"relevance": 5}, fulltext_used=True)
    with pytest.raises(ValueError, match="范围"):
        compute_score(rubric(), levels(6, 5, 5, 5), fulltext_used=True)
    with pytest.raises(ValueError, match="未知"):
        compute_score(rubric(), {**levels(5, 5, 5, 5), "extra": 1}, fulltext_used=True)


def test_fulltext_constraint_nulls_only_fulltext_dimensions_and_copies():
    original = levels(5, 4, 5, 5)
    constrained = apply_fulltext_constraint(rubric(), original, fulltext_used=False)
    assert constrained == levels(5, 4, None, None)
    assert original == levels(5, 4, 5, 5)
    assert apply_fulltext_constraint(rubric(), original, fulltext_used=True) == original


def test_weight_change_only_changes_policy_hash():
    data = raw()
    data["dimensions"][0]["weight"] = 30
    data["dimensions"][1]["weight"] = 30
    changed = Rubric.model_validate(data)
    assert changed.policy_hash != rubric().policy_hash
    assert changed.semantic_hash == rubric().semantic_hash


def test_anchor_change_only_changes_semantic_hash():
    data = raw()
    data["dimensions"][0]["anchors"]["3"] = "新的锚点"
    changed = Rubric.model_validate(data)
    assert changed.semantic_hash != rubric().semantic_hash
    assert changed.policy_hash == rubric().policy_hash


def test_assessment_schema_never_asks_llm_for_total_score():
    schema = rubric().assessment_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"dimensions", "goal_links"}
    dims = schema["properties"]["dimensions"]["properties"]
    assert set(dims) == {"relevance", "novelty", "evidence", "reusability"}
    assert dims["relevance"]["properties"]["level"]["maximum"] == 5


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["dimensions"][0].update(weight=10), "权重总和"),
        (lambda d: d["gates"][0].update(dimension="nope"), "门槛"),
        (lambda d: d["gates"][0].update(min_level=9), "门槛"),
        (lambda d: d["tiers"].pop(), "min_score 为 0"),
        (lambda d: d["provisional"].update(tier="digest"), "provisional"),
        (lambda d: d["dimensions"][0]["anchors"].update({"9": "x"}), "锚点"),
        (lambda d: d["dimensions"].append(dict(d["dimensions"][0], weight=1)), "维度"),
        (lambda d: d.update(typo=1), "typo"),
    ],
)
def test_invalid_rubric_fails_fast(mutate, message):
    data = raw()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        Rubric.model_validate(data)


def test_gate_on_fulltext_dimension_is_rejected():
    """门槛维度只读摘要时必然为空，会让所有摘要论文进不了 need_fulltext。"""
    data = raw()
    data["gates"] = [{"dimension": "evidence", "min_level": 2}]
    with pytest.raises(ValidationError, match="requires_fulltext"):
        Rubric.model_validate(data)


def test_scoring_rule_version_is_part_of_policy_hash(monkeypatch):
    """算分规则本身变了，旧分数要按新规则零费用重算。"""
    before = rubric().policy_hash
    monkeypatch.setattr(module, "SCORING_VERSION", module.SCORING_VERSION + 1)
    assert rubric().policy_hash != before
