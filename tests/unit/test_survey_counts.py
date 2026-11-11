"""调研计数与论文集合的纯一致性约束。"""

import pytest
from pydantic import ValidationError

from litgleaner.domain.survey import NewSurveyRound, SurveyWork, coverage_ratio


def _round(**changes):
    return NewSurveyRound(
        **(
            {
                "topic_id": "topic",
                "topic_hash": "hash",
                "run_id": "run",
                "iteration": 1,
                "plan_id": "plan",
                "started_at": "t",
                "finished_at": "t2",
                "candidates": 2,
                "new_papers": 1,
                "passed": 1,
                "new_passed": 1,
                "undetermined": 0,
                "cost_usd": 0,
            }
            | changes
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"new_passed": 2},
        {"passed": 3},
        {"new_papers": 0},
        {"new_papers": 3},
        {"undetermined": 3},
        {"candidates": True},
        {"passed": "1"},
        {"iteration": False},
        {"contributions": {"source": -1}},
        {"contributions": {"source": True}},
        {"paper_ids": ("P", "P")},
    ],
)
def test_round_rejects_inconsistent_counts_coercion_and_duplicate_papers(changes):
    with pytest.raises(ValidationError):
        _round(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"paper_ids": ("P", "P")},
        {"new_paper_ids": ("unknown",)},
        {"new_paper_ids": ("P", "P")},
        {"paper_sources": {"unknown": ("source",)}},
    ],
)
def test_work_rejects_duplicate_or_unrelated_paper_sets(changes):
    values = {
        "run_id": "run",
        "iteration": 1,
        "plan_id": "plan",
        "scope_id": "scope",
        "topic_id": "topic",
        "topic_hash": "hash",
        "started_at": "t",
        "paper_ids": ("P",),
        "new_paper_ids": ("P",),
        "paper_sources": {"P": ("source",)},
    }
    with pytest.raises(ValidationError):
        SurveyWork(**(values | changes))


def test_coverage_keeps_undetermined_distinct_from_zero_and_accepts_empty_round():
    assert coverage_ratio(1, 2) == 0.5
    assert coverage_ratio(0, 2) == 0
    assert coverage_ratio(1, 2, 1) is coverage_ratio(0, 0) is None
    assert _round(candidates=0, new_papers=0, passed=0, new_passed=0).candidates == 0
