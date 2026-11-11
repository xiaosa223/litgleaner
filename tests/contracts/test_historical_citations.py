"""两来源历史被引显式日期与分页，全部通过 MockTransport 验证。"""

from datetime import date

import httpx
import pytest
from pydantic import ValidationError
from tests.helpers.http import Router

from litgleaner.adapters.sources.openalex import OpenAlexSource
from litgleaner.adapters.sources.semantic_scholar import SemanticScholarSource
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.query import CitationWatch, HistoricalCitations, query_from_payload


def query(**changes):
    return HistoricalCitations(
        **{
            "dois": ("10.1000/seed",),
            "since": date(1990, 1, 1),
            "until": date(2020, 12, 31),
            "limit": 3,
            **changes,
        }
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"dois": ()},
        {"dois": ("bad",)},
        {"limit": True},
        {"limit": 1.0},
        {"limit": "2"},
        {"limit": 0},
        {"limit": 1001},
        {"since": date(2021, 1, 1)},
    ],
)
def test_history_citations_validation_is_distinct_from_daily_query(changes):
    with pytest.raises(ValidationError):
        query(**changes)


def test_roundtrip_hash_and_daily_compatibility():
    original = query(dois=("10.1000/b", "10.1000/a"))
    assert query_from_payload(original.model_dump(mode="json")) == original
    assert original.hash == query(dois=("10.1000/a", "10.1000/b")).hash
    assert original.hash != CitationWatch(dois=("10.1000/a", "10.1000/b")).hash


def oa_work(index, published="2001-01-01"):
    return {
        "id": f"https://openalex.org/W{index}",
        "title": f"Citing paper {index}",
        "publication_date": published,
        "type": "article",
    }


def oa_source(router, **options):
    return OpenAlexSource(
        "oa",
        {"min_interval_seconds": 0, **options},
        contact_email=None,
        transport=router.transport,
        today=lambda: date(2026, 11, 11),
    )


def test_openalex_historical_citations_resolve_seed_and_page_explicit_dates():
    pages = iter(
        [
            {"meta": {"count": 4}, "results": [oa_work(1), oa_work(2, "2026-01-01")]},
            {"meta": {"count": 4}, "results": [oa_work(3), oa_work(4)]},
        ]
    )
    router = Router().reply("/works/doi:", json_body={"id": "https://openalex.org/W9"})
    router.on("/works", lambda _: httpx.Response(200, json=next(pages)))
    found = oa_source(router, per_page=2).fetch(query(), "2026-11-10")
    assert [r.title for r in found.records] == ["Citing paper 1", "Citing paper 3"]
    params = router.params(1)
    assert (
        params["filter"]
        == "from_publication_date:1990-01-01,to_publication_date:2020-12-31,cites:W9"
    )
    assert (
        params["sort"] == "publication_date:desc" and "search.title_abstract_keywords" not in params
    )
    assert router.params(2)["page"] == "2"
    assert found.truncated and found.next_cursor is None and found.raw


def test_openalex_unknown_seed_is_a_reported_gap_and_config_limit_is_enforced():
    missing = oa_source(Router()).fetch(query(), None)
    assert missing.records == () and any("未覆盖" in warning for warning in missing.warnings)
    router = Router().reply("/works/doi:", json_body={"id": "https://openalex.org/W9"})
    router.reply(
        "/works", json_body={"meta": {"count": 3}, "results": [oa_work(i) for i in range(3)]}
    )
    found = oa_source(router, max_records=1).fetch(query(), None)
    assert len(found.records) == 1 and found.truncated
    assert any("SourceOptions.max_records=1" in warning for warning in found.warnings)


def citing(index, published="2001-01-01"):
    return {
        "citingPaper": {
            "paperId": str(index),
            "title": f"Citing paper {index}",
            "publicationDate": published,
            "year": 2001,
        }
    }


def s2_source(router, **options):
    return SemanticScholarSource(
        "s2",
        {"min_interval_seconds": 0, **options},
        contact_email=None,
        transport=router.transport,
        today=lambda: date(2026, 11, 11),
    )


def test_semantic_history_citations_have_bounded_offset_and_explicit_date_range():
    pages = iter(
        [
            {"offset": 0, "next": 2, "data": [citing(1), citing(2, "2026-01-01")]},
            {"offset": 2, "next": 4, "data": [citing(3), citing(4)]},
        ]
    )
    router = Router().on("/citations", lambda _: httpx.Response(200, json=next(pages)))
    found = s2_source(router, page_size=2).fetch(query(), "2026-11-10")
    assert [r.title for r in found.records] == ["Citing paper 1", "Citing paper 3"]
    assert router.params(0)["publicationDateOrYear"] == "1990-01-01:2020-12-31"
    assert [router.params(i)["limit"] for i in range(2)] == ["2", "1"]
    assert [router.params(i)["offset"] for i in range(2)] == ["0", "2"]
    assert found.next_cursor is None and found.truncated and found.raw


def test_semantic_limit_is_shared_by_all_dois_and_missing_seed_is_reported():
    router = Router().reply("/citations", json_body={"offset": 0, "data": [citing(1), citing(2)]})
    found = s2_source(router, max_records=1).fetch(query(dois=("10.1000/a", "10.1000/b")), None)
    assert len(router.requests) == 1 and len(found.records) == 1 and found.truncated
    assert any("SourceOptions.max_records=1" in warning for warning in found.warnings)
    missing = s2_source(Router()).fetch(query(), None)
    assert missing.records == () and any("未覆盖" in warning for warning in missing.warnings)


@pytest.mark.parametrize(
    "body",
    [
        {"data": "broken"},
        {"offset": 1, "data": []},
        {"data": [], "next": 1},
        {"data": [citing(1)], "next": True},
        {"data": [citing(1)], "next": 0},
    ],
)
def test_semantic_malformed_page_has_typed_failure(body):
    with pytest.raises(SchemaChanged):
        s2_source(Router().reply("/citations", json_body=body)).fetch(query(), None)
