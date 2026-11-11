"""历史查询显式限定范围，不进入日常计划，序列化后仍保留幂等键。"""

from datetime import date

import pytest
from pydantic import ValidationError

from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.query import (
    HistoricalSearch,
    KeywordSearch,
    plan_queries,
    query_from_payload,
)


def query(**changes):
    return HistoricalSearch(
        **{
            "terms": ("graph learning",),
            "since": date(1990, 1, 1),
            "until": date(2025, 12, 31),
            "category": "highly_cited",
            **changes,
        }
    )


@pytest.mark.parametrize("limit", [0, -1, 1001, True, 2.0, "2"])
def test_limit_is_a_strict_bounded_integer(limit):
    with pytest.raises(ValidationError):
        query(limit=limit)


@pytest.mark.parametrize("terms", [(), ("",), (" \t",), ("valid", "")])
def test_at_least_one_nonblank_term_is_required(terms):
    with pytest.raises(ValidationError):
        query(terms=terms)


def test_date_order_and_category_are_validated():
    with pytest.raises(ValidationError):
        query(since=date(2026, 1, 1))
    with pytest.raises(ValidationError):
        query(category="guessed_reviews")
    assert query(since=date(2025, 12, 31)).until == date(2025, 12, 31)


def test_payload_roundtrip_and_order_insensitive_hash_preserve_explicit_range():
    first = query(terms=("a", "b"), exclude=("y", "x"), limit=1000)
    assert query_from_payload(first.model_dump(mode="json")) == first
    assert first.hash == query(terms=("b", "a"), exclude=("x", "y"), limit=1000).hash
    assert first.hash != query(terms=("a", "b"), exclude=("y", "x"), limit=999).hash
    assert not any(isinstance(item, HistoricalSearch) for item in plan_queries((), {}))


def test_existing_keyword_hash_is_unchanged():
    existing = KeywordSearch(terms=("x",), exclude=("y",))
    assert existing.hash == stable_hash(
        {"kind": "keyword_search", "terms": ["x"], "exclude": ["y"]}
    )
