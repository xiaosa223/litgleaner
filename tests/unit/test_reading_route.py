"""阅读路线只使用明确的元数据和当前课题的判断。"""

from dataclasses import replace
from datetime import date

import pytest

from litgleaner.domain.paper import Paper
from litgleaner.domain.reading_route import RouteCandidate, order_route, recent_since


def candidate(
    id_: str,
    *,
    types: tuple[str, ...] = (),
    citations: int = 0,
    score: int | None = None,
    published: str | None = None,
) -> RouteCandidate:
    paper = Paper(id_, id_, id_, None, (), None, None, published, "2026-01-01", types, None)
    return RouteCandidate(paper, citations, score)


def test_route_uses_four_real_signals_and_does_not_repeat_papers():
    papers = (
        candidate("review", types=("Review",), citations=20, score=100),
        candidate("foundation", citations=5, score=95, published="2001-01-01"),
        candidate("representative", score=90, published="2010-01-01"),
        candidate("latest", published="2026-10-01"),
        candidate("unknown"),
    )
    result = order_route(papers, date(2026, 11, 11), limit_per_group=1)
    assert [(group.kind, group.paper_ids) for group in result.groups] == [
        ("reviews", ("review",)),
        ("foundations", ("foundation",)),
        ("representatives", ("representative",)),
        ("recent", ("latest",)),
    ]
    assert result.ungrouped == ("unknown",)


def test_review_title_and_external_citation_count_do_not_create_route_facts():
    item = candidate("A survey of methods")
    item = replace(item, paper=replace(item.paper, citation_count=10000))
    result = order_route((item,), date(2026, 11, 11))
    assert all(not group.paper_ids for group in result.groups)
    assert result.ungrouped == ("A survey of methods",)


def test_recent_is_closed_calendar_interval_and_ignores_unknown_or_future_dates():
    candidates = tuple(
        candidate(id_, published=published)
        for id_, published in (
            ("old", "2024-11-10"),
            ("boundary", "2024-11-11"),
            ("today", "2026-11-11"),
            ("future", "2026-11-12"),
            ("broken", "unknown"),
        )
    )
    route = order_route(candidates, date(2026, 11, 11))
    assert route.groups[-1].paper_ids == ("today", "boundary")
    assert recent_since(date(2024, 2, 29)) == date(2022, 2, 28)


def test_stable_ties_and_caps_leave_omissions_visible():
    papers = tuple(candidate(id_, score=0) for id_ in ("c", "b", "a"))
    forward = order_route(papers, date(2026, 11, 11), limit_per_group=2)
    assert forward == order_route(tuple(reversed(papers)), date(2026, 11, 11), limit_per_group=2)
    assert forward.groups[2].paper_ids == ("a", "b")
    assert forward.ungrouped == ("c",)


@pytest.mark.parametrize("limit", [0, 101])
def test_invalid_group_limit_is_rejected(limit):
    with pytest.raises(ValueError):
        order_route((), date(2026, 11, 11), limit_per_group=limit)


def test_duplicate_ids_and_negative_topic_counts_are_rejected():
    paper = candidate("same")
    with pytest.raises(ValueError):
        order_route((paper, paper), date(2026, 11, 11))
    with pytest.raises(ValueError):
        order_route((candidate("bad", citations=-1),), date(2026, 11, 11))
