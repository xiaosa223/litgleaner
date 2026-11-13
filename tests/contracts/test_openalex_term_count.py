"""按官方 OpenAlex 参数构造模拟响应，未请求真实来源：计数只取 meta.count，不读论文。"""

from datetime import date

import pytest
from tests.helpers.http import Router

from litgleaner.adapters.sources.openalex import OpenAlexSource
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.query import TermCount

QUERY = TermCount(
    terms=("EEG foundation model", "brain foundation model"),
    exclude=("fMRI",),
    since=date(2025, 11, 13),
    until=date(2026, 11, 13),
)


def source(router):
    return OpenAlexSource(
        "openalex",
        {"min_interval_seconds": 0},
        contact_email=None,
        transport=router.transport,
        today=lambda: date(2026, 11, 13),
    )


def test_term_count_asks_for_one_row_and_reads_the_reported_total():
    router = Router().reply("/works", json_body={"meta": {"count": 348}, "results": [{"id": "W1"}]})
    result = source(router).fetch(QUERY, None)
    params = router.params()
    assert params["search.title_abstract_keywords"] == (
        '("EEG foundation model" OR "brain foundation model") NOT ("fMRI")'
    )
    assert params["filter"] == "from_publication_date:2025-11-13,to_publication_date:2026-11-13"
    assert (params["per_page"], params["select"]) == ("1", "id")
    assert result.total == 348 and result.records == () and result.next_cursor is None


@pytest.mark.parametrize("body", [{"results": []}, {"meta": {"count": "many"}}, {"meta": {}}])
def test_term_count_without_a_numeric_total_is_a_schema_change(body):
    router = Router().reply("/works", json_body=body)
    with pytest.raises(SchemaChanged):
        source(router).fetch(QUERY, None)
