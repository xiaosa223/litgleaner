"""综述骨架必须绑定确认输入、目标及不可变事实附件。"""

import pytest
from pydantic import ValidationError

from litgleaner.domain.outline import (
    OutlineGap,
    OutlineGroup,
    OutlinePaper,
    TopicOutline,
    TopicOutlineSettings,
    validate_outline,
)


def group(**changes):
    return {
        "name": "方法组",
        "description": "短说明",
        "goal_ids": ["g1"],
        "paper_ids": ["p1"],
        **changes,
    }


def gap(**changes):
    return {"description": "当前已读集合缺少该方法的数据比较", "paper_ids": ["p1"], **changes}


def paper(id_="p1", **changes):
    return OutlinePaper(
        paper_id=id_,
        title="Saved title " + id_,
        table_markdown="",
        bibtex="",
        csl_json="[]",
        needs_verification=True,
        **changes,
    )


def outline_data(**changes):
    return {
        "id": "outline-1",
        "topic_id": "topic",
        "topic_hash": "topic-hash",
        "input_hash": "input-hash",
        "producer_version": "outline-v1",
        "model": "fake",
        "reading_language": "zh-CN",
        "token_budget": 12000,
        "created_at": "2026-11-11T00:00:00Z",
        "scope_id": "outline:topic",
        "considered_paper_ids": ("p1", "p2", "omitted"),
        "input_paper_ids": ("p1", "p2"),
        "source_hashes": {"p1": "h1", "p2": "h2"},
        "goal_ids": ("g1",),
        "groups": (OutlineGroup(**group()),),
        "gaps": (OutlineGap(**gap()),),
        "papers": (paper("p1"), paper("p2")),
        "ungrouped": ("p2", "omitted"),
        **changes,
    }


def test_outline_models_are_frozen_and_preserve_attachment_snapshot():
    result = TopicOutline(**outline_data())
    assert result.papers[0].title == "Saved title p1"
    assert result.warnings == ()
    assert TopicOutline.model_validate(result.model_dump()) == result
    for item, name in ((result, "model"), (result.groups[0], "name"), (result.papers[0], "title")):
        with pytest.raises(ValidationError, match="frozen"):
            setattr(item, name, "changed")


@pytest.mark.parametrize("value", [511, 64001, True, "12000", 12000.0])
def test_outline_budget_is_strict_and_bounded(value):
    with pytest.raises(ValidationError):
        TopicOutlineSettings(token_budget=value)
    with pytest.raises(ValidationError):
        TopicOutline(**outline_data(token_budget=value))


def test_outline_budget_defaults_and_valid_boundaries():
    assert TopicOutlineSettings().token_budget == 12000
    assert TopicOutlineSettings(token_budget=512).token_budget == 512
    assert TopicOutlineSettings(token_budget=64000).token_budget == 64000


@pytest.mark.parametrize(
    "changes",
    [
        {"considered_paper_ids": ("p1", "p1", "p2", "omitted")},
        {"input_paper_ids": ("p1", "p1")},
        {"considered_paper_ids": ("p1", "omitted")},
        {"source_hashes": {"p1": "h1"}},
        {"source_hashes": {"p1": "h1", "p2": " "}},
        {"groups": (OutlineGroup(**group(paper_ids=["unseen"])),)},
        {"groups": (OutlineGroup(**group()), OutlineGroup(**group()))},
        {"ungrouped": ("p2",)},
        {"ungrouped": ("p2", "omitted", "omitted")},
        {"papers": (paper("p2"), paper("p1"))},
        {"papers": (paper("p1"),)},
        {"goal_ids": ("g1", "g1")},
        {"goal_ids": ()},
        {"gaps": (OutlineGap(**gap(paper_ids=["omitted"])),)},
        {"gaps": tuple(OutlineGap(**gap()) for _ in range(13))},
    ],
)
def test_outline_rejects_invalid_partitions_or_snapshot_attachments(changes):
    with pytest.raises(ValidationError):
        TopicOutline(**outline_data(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"name": " "},
        {"description": "\n"},
        {"name": "x" * 121},
        {"description": "x" * 601},
        {"goal_ids": ["bad goal"]},
        {"goal_ids": ["g1", "g1"]},
        {"paper_ids": []},
        {"paper_ids": ["p1", "p1"]},
    ],
)
def test_group_rejects_blank_unbounded_or_duplicate_fields(changes):
    with pytest.raises(ValidationError):
        OutlineGroup(**group(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"description": " "},
        {"description": "x" * 501},
        {"paper_ids": []},
        {"paper_ids": ["p1", "p1"]},
    ],
)
def test_gap_rejects_blank_unbounded_or_duplicate_fields(changes):
    with pytest.raises(ValidationError):
        OutlineGap(**gap(**changes))


def test_validate_filters_unknown_ids_and_goals_then_keeps_first_accepted_assignment():
    raw = {
        "groups": [
            group(name=" ", paper_ids=["p1"]),
            group(
                name=" A ",
                description=" description ",
                paper_ids=["p1", "unknown", "p1"],
                goal_ids=["g1", "bad goal", "not-current", "g1"],
            ),
            group(name="B", paper_ids=["p1", "p2"]),
        ],
        "gaps": [],
    }
    groups, gaps, warnings = validate_outline(raw, ("p1", "p2"), ("g1",), ("p1", "p2"))
    assert [(item.name, item.paper_ids, item.goal_ids) for item in groups] == [
        ("A", ("p1",), ("g1",)),
        ("B", ("p2",), ("g1",)),
    ]
    assert groups[0].description == "description"
    assert not gaps and warnings and len(warnings) == len(set(warnings))


def test_gap_with_any_unsent_or_incomplete_paper_is_dropped_whole_without_salvage():
    raw = {
        "groups": [],
        "gaps": [
            gap(paper_ids=["p1", "not-sent"]),
            gap(paper_ids=["p1", "p2"]),
            gap(paper_ids=["p1", "p1"]),
            gap(paper_ids=["p1"]),
        ],
    }
    groups, gaps, warnings = validate_outline(raw, ("p1", "p2"), (), ("p1", "not-sent"))
    assert not groups
    assert len(gaps) == 2 and all(item.paper_ids == ("p1",) for item in gaps)
    assert any("整条" in warning for warning in warnings)


@pytest.mark.parametrize("item", [None, 42, "raw", {}, {"paper_ids": ["p1"]}])
def test_validate_drops_malformed_candidates_instead_of_guessing(item):
    groups, gaps, warnings = validate_outline(
        {"groups": [item], "gaps": [item]}, ("p1",), (), ("p1",)
    )
    assert not groups and not gaps and warnings


def test_validate_bounds_outputs_and_does_not_reserve_ids_of_rejected_groups():
    ids = tuple(f"p{index}" for index in range(13))
    raw = {
        "groups": [group(paper_ids=[id_], goal_ids=[]) for id_ in ids],
        "gaps": [gap(paper_ids=[id_]) for id_ in ids],
    }
    groups, gaps, warnings = validate_outline(raw, ids, (), ids)
    assert len(groups) == len(gaps) == 12
    assert all(item.paper_ids == (id_,) for item, id_ in zip(groups, ids, strict=False))
    assert warnings


def test_no_invention_when_model_returns_no_valid_groups_or_gap_evidence():
    assert validate_outline({"groups": [], "gaps": []}, ("p1",), (), ()) == ((), (), ())
    groups, gaps, warnings = validate_outline({"groups": [], "gaps": [gap()]}, ("p1",), (), ())
    assert not groups and not gaps and warnings
