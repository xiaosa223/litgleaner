"""Semantic Scholar 历史搜索真实参数与有界分页；使用模拟响应。"""

from datetime import date

import httpx
import pytest
from tests.helpers.http import Router

from litgleaner.adapters.sources.semantic_scholar import SemanticScholarSource
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.query import HistoricalSearch


def query(category="highly_cited", **changes):
    return HistoricalSearch(
        terms=("graph learning",),
        exclude=("chemistry",),
        since=date(1990, 1, 1),
        until=date(2020, 12, 31),
        category=category,
        **changes,
    )


def paper(index, *, kinds=None, count=10, published="2001-01-01"):
    return {
        "paperId": str(index),
        "title": f"Valid paper {index}",
        "year": 2001,
        "publicationDate": published,
        "publicationTypes": ["Review"] if kinds is None else kinds,
        "citationCount": count,
    }


def source(router, **options):
    return SemanticScholarSource(
        "semantic_scholar",
        {"min_interval_seconds": 0, **options},
        contact_email=None,
        transport=router.transport,
        today=lambda: date(2026, 11, 11),
    )


@pytest.mark.parametrize(
    "category,sort",
    [
        ("reviews", "publicationDate:desc"),
        ("highly_cited", "citationCount:desc"),
        ("recent", "publicationDate:desc"),
    ],
)
def test_history_uses_bulk_real_review_filter_dates_sort_and_no_daily_cursor(category, sort):
    router = Router().reply("/paper/search/bulk", json_body={"data": [paper(1)], "total": 1})
    result = source(router).fetch(query(category), "2026-11-10")
    params = router.params()
    assert params["query"] == '("graph learning") -("chemistry")'
    assert params["publicationDateOrYear"] == "1990-01-01:2020-12-31"
    assert (params.get("publicationTypes") == "Review") == (category == "reviews")
    assert params["sort"] == sort and "limit" not in params and "token" not in params
    assert {"publicationTypes", "citationCount"} <= set(params["fields"].split(","))
    assert result.next_cursor is None and not result.truncated
    assert result.records[0].publication_types == ("Review",)
    assert result.records[0].citation_count == 10


def test_raw_entry_limit_across_tokens_includes_duplicates_and_invalid_entries():
    pages = iter(
        [
            {"data": [paper(1), paper(1), paper(2)], "token": "NEXT", "total": 100},
            {"data": [None, paper(3), paper(4)], "token": "AFTER", "total": 100},
        ]
    )
    router = Router().on("/paper/search/bulk", lambda _: httpx.Response(200, json=next(pages)))
    result = source(router).fetch(query(limit=5), None)
    assert [record.title for record in result.records] == [
        "Valid paper 1",
        "Valid paper 2",
        "Valid paper 3",
    ]
    assert len(router.requests) == 2 and router.params(1)["token"] == "NEXT"
    assert result.next_cursor is None and result.truncated
    assert any("上限" in warning for warning in result.warnings)


def test_review_type_and_date_are_verified_without_guessing_from_title():
    items = [
        paper(1, kinds=["JournalArticle"]),
        paper(2),
        paper(3, published="2026-01-01"),
        paper(4, kinds=[]),
    ]
    items[0]["title"] = "A survey and review of the field"
    router = Router().reply("/paper/search/bulk", json_body={"data": items, "total": 4})
    result = source(router).fetch(query("reviews"), None)
    assert [record.title for record in result.records] == ["Valid paper 2"]
    assert result.warnings


@pytest.mark.parametrize("count,kinds", [(True, "Review"), (-1, {}), ("20", False), (3.0, 42)])
def test_optional_metadata_bad_types_cannot_discard_a_valid_paper(count, kinds):
    router = Router().reply(
        "/paper/search/bulk", json_body={"data": [paper(1, kinds=kinds, count=count)]}
    )
    result = source(router).fetch(query(), None)
    assert len(result.records) == 1
    assert result.records[0].citation_count is None and result.records[0].publication_types == ()
    assert any("被引数" in warning for warning in result.warnings)


def test_year_only_dates_follow_official_january_first_semantics_with_warning():
    router = Router().reply("/paper/search/bulk", json_body={"data": [paper(1, published=None)]})
    result = source(router).fetch(query("recent"), None)
    assert result.records[0].published_at == date(2001, 1, 1)
    assert any("年份" in warning for warning in result.warnings)


def test_high_citation_sort_uses_actual_counts_with_unknown_last():
    router = Router().reply(
        "/paper/search/bulk",
        json_body={"data": [paper(1, count=None), paper(2, count=1), paper(3, count=50)]},
    )
    result = source(router).fetch(query(), None)
    assert [record.citation_count for record in result.records] == [50, 1, None]


@pytest.mark.parametrize("token", ["REPEAT", 123, False])
def test_invalid_or_repeated_tokens_are_typed_schema_failures(token):
    router = Router().reply("/paper/search/bulk", json_body={"data": [paper(1)], "token": token})
    with pytest.raises(SchemaChanged):
        source(router).fetch(query(), None)


def test_configured_raw_entry_limit_cannot_be_bypassed_by_history_query():
    router = Router().reply("/paper/search/bulk", json_body={"data": [paper(i) for i in range(5)]})
    result = source(router, max_records=2).fetch(query(limit=1000), None)
    assert len(result.records) == 2 and result.truncated and len(router.requests) == 1
    assert any("SourceOptions.max_records=2" in warning for warning in result.warnings)


def test_recent_date_order_has_inclusive_bounds_and_no_fake_date_for_missing_year():
    items = [
        paper(1, published="1990-01-01"),
        paper(2, published="2020-12-31"),
        paper(3, published="2021-01-01"),
        paper(4, published=None),
    ]
    items[3]["year"] = "unknown"
    router = Router().reply("/paper/search/bulk", json_body={"data": items})
    result = source(router).fetch(query("recent"), None)
    assert [record.published_at for record in result.records] == [
        date(2020, 12, 31),
        date(1990, 1, 1),
    ]


def test_mixed_publication_types_keep_only_real_nonblank_string_values():
    item = paper(1, kinds=["Review", {"invented": "type"}, False, 10, " ", "Review"])
    router = Router().reply("/paper/search/bulk", json_body={"data": [item]})
    result = source(router).fetch(query("reviews"), None)
    assert result.records[0].publication_types == ("Review",)


def test_empty_page_with_a_token_is_an_explicit_failure():
    router = Router().reply("/paper/search/bulk", json_body={"data": [], "token": "NEXT"})
    with pytest.raises(SchemaChanged):
        source(router).fetch(query(), None)


def test_empty_result_is_complete_and_zero_is_a_real_citation_count():
    empty = Router().reply("/paper/search/bulk", json_body={"data": []})
    result = source(empty).fetch(query(), None)
    assert result.records == () and not result.truncated and result.next_cursor is None
    zero = Router().reply("/paper/search/bulk", json_body={"data": [paper(1, count=0)]})
    assert source(zero).fetch(query(), None).records[0].citation_count == 0
