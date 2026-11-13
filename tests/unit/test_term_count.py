"""检索词命中计数的查询（第 7.2 节）：一组同义词一次查询，有界日期窗口。"""

from datetime import date

import pytest
from pydantic import ValidationError

from litgleaner.domain.query import TermCount, query_from_payload


def test_term_count_round_trips_as_a_query_payload():
    query = TermCount(
        terms=("EEG", "electroencephalography"),
        exclude=("fMRI",),
        since=date(2025, 11, 13),
        until=date(2026, 11, 13),
    )
    assert query_from_payload(query.model_dump(mode="json")) == query


@pytest.mark.parametrize(
    "change",
    [
        {"terms": ()},
        {"terms": ("x",) * 10},
        {"terms": (" ",)},
        {"since": date(2027, 1, 1)},
    ],
)
def test_term_count_rejects_empty_oversized_or_reversed_queries(change):
    fields = {"terms": ("EEG",), "since": date(2025, 1, 1), "until": date(2026, 1, 1)}
    with pytest.raises(ValidationError):
        TermCount(**{**fields, **change})
