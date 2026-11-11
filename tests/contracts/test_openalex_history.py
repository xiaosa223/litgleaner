"""按官方 OpenAlex 参数构造模拟响应，未请求真实来源。"""

from datetime import date

import httpx
import pytest
from tests.helpers.http import Router

from litgleaner.adapters.sources.openalex import OpenAlexSource
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


def work(index, *, kind="article", count=10, published="2001-01-01"):
    return {
        "id": f"https://openalex.org/W{index}",
        "title": f"Valid paper {index}",
        "publication_date": published,
        "type": kind,
        "cited_by_count": count,
    }


def source(router, *, keyed=False, **options):
    return OpenAlexSource(
        "openalex",
        {"min_interval_seconds": 0, **options},
        contact_email=None,
        api_key="test-history-key" if keyed else None,
        transport=router.transport,
        today=lambda: date(2026, 11, 11),
    )


@pytest.mark.parametrize(
    "category,sort",
    [
        ("reviews", "publication_date:desc"),
        ("highly_cited", "cited_by_count:desc"),
        ("recent", "publication_date:desc"),
    ],
)
def test_history_uses_explicit_range_real_review_type_and_documented_sort(category, sort):
    items = [work(1, kind="review")]
    router = Router().reply("/works", json_body={"meta": {"count": 1}, "results": items})
    result = source(router).fetch(query(category), "2026-11-10")
    params = router.params()
    assert params["search.title_abstract_keywords"] == '("graph learning") NOT ("chemistry")'
    assert "from_publication_date:1990-01-01,to_publication_date:2020-12-31" in params["filter"]
    assert ("type:review" in params["filter"]) == (category == "reviews")
    assert params["sort"] == sort
    assert {"type", "cited_by_count"} <= set(params["select"].split(","))
    assert result.next_cursor is None and len(result.records) == 1
    assert result.records[0].publication_types == ("review",)
    assert result.records[0].citation_count == 10
    assert not result.truncated


@pytest.mark.parametrize("keyed", [False, True])
def test_raw_entry_limit_is_enforced_across_pages_including_duplicates_and_invalid_rows(keyed):
    pages = iter(
        [
            {"meta": {"count": 20, "next_cursor": "NEXT"}, "results": [work(1), work(1), work(2)]},
            {"meta": {"count": 20, "next_cursor": "AFTER"}, "results": [None, work(3), work(4)]},
        ]
    )
    router = Router().on("/works", lambda _: httpx.Response(200, json=next(pages)))
    result = source(router, keyed=keyed, per_page=3).fetch(query(limit=5), None)
    assert [record.title for record in result.records] == [
        "Valid paper 1",
        "Valid paper 2",
        "Valid paper 3",
    ]
    assert len(router.requests) == 2 and result.truncated and result.next_cursor is None
    assert any("上限" in warning for warning in result.warnings)
    if keyed:
        assert [router.params(i)["cursor"] for i in range(2)] == ["*", "NEXT"]
    else:
        assert [router.params(i)["page"] for i in range(2)] == ["1", "2"]


def test_reviews_require_real_type_not_title_and_dates_are_checked_locally():
    items = [
        work(1, kind="article"),
        work(2, kind="review"),
        work(3, kind="review", published="2026-01-01"),
        work(4, kind=None),
    ]
    items[0]["title"] = "A comprehensive review of graph learning"
    router = Router().reply("/works", json_body={"meta": {"count": 4}, "results": items})
    result = source(router).fetch(query("reviews"), None)
    assert [record.title for record in result.records] == ["Valid paper 2"]
    assert result.warnings


@pytest.mark.parametrize("count,kind", [(True, {}), (-1, 42), ("20", None), (3.0, [])])
def test_bad_optional_metadata_cannot_drop_an_otherwise_valid_paper(count, kind):
    router = Router().reply(
        "/works", json_body={"meta": {"count": 1}, "results": [work(1, kind=kind, count=count)]}
    )
    result = source(router).fetch(query(), None)
    assert len(result.records) == 1
    assert result.records[0].citation_count is None
    assert result.records[0].publication_types == ()
    assert any("被引数" in warning for warning in result.warnings)


def test_highly_cited_returns_real_counts_in_descending_order_with_unknown_last():
    items = [work(1, count=None), work(2, count=3), work(3, count=100)]
    router = Router().reply("/works", json_body={"meta": {"count": 3}, "results": items})
    result = source(router).fetch(query(), None)
    assert [record.citation_count for record in result.records] == [100, 3, None]


def test_repeated_cursor_and_malformed_page_raise_typed_schema_errors():
    router = Router().reply(
        "/works", json_body={"meta": {"count": 9, "next_cursor": "*"}, "results": [work(1)]}
    )
    with pytest.raises(SchemaChanged):
        source(router, keyed=True).fetch(query(), None)
    broken = Router().reply("/works", json_body={"meta": {}, "results": {}})
    with pytest.raises(SchemaChanged):
        source(broken).fetch(query(), None)


def test_configured_raw_entry_limit_cannot_be_bypassed_by_a_larger_history_limit():
    items = [work(index) for index in range(5)]
    router = Router().reply("/works", json_body={"meta": {"count": 5}, "results": items})
    result = source(router, max_records=2).fetch(query(limit=1000), None)
    assert len(result.records) == 2 and result.truncated and len(router.requests) == 1
    assert any("SourceOptions.max_records=2" in warning for warning in result.warnings)


def test_recent_uses_real_date_order_and_both_range_endpoints_are_inclusive():
    items = [
        work(1, published="1990-01-01"),
        work(2, published="2020-12-31"),
        work(3, published="1989-12-31"),
        work(4, published="2021-01-01"),
    ]
    router = Router().reply("/works", json_body={"meta": {"count": 4}, "results": items})
    result = source(router).fetch(query("recent"), None)
    assert [record.published_at for record in result.records] == [
        date(2020, 12, 31),
        date(1990, 1, 1),
    ]


@pytest.mark.parametrize("keyed,meta", [(False, {"count": 1}), (True, {"next_cursor": "NEXT"})])
def test_empty_page_cannot_silently_claim_complete_coverage(keyed, meta):
    router = Router().reply("/works", json_body={"meta": meta, "results": []})
    with pytest.raises(SchemaChanged):
        source(router, keyed=keyed).fetch(query(), None)


def test_empty_result_is_complete_and_zero_citations_are_real_metadata():
    empty = Router().reply("/works", json_body={"meta": {"count": 0}, "results": []})
    result = source(empty).fetch(query(), None)
    assert result.records == () and not result.truncated and result.next_cursor is None
    zero = Router().reply("/works", json_body={"meta": {"count": 1}, "results": [work(1, count=0)]})
    assert source(zero).fetch(query(), None).records[0].citation_count == 0
