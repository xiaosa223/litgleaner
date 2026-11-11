"""参考文献响应按官方结构构造，通过 MockTransport 重放；不调用真实 API。"""

import copy
import json
from datetime import date

import httpx
import pytest
from tests.helpers.http import Router, fixture_json

from litgleaner.adapters.sources.openalex import OpenAlexSource
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import ReferenceList, plan_queries, query_from_payload
from litgleaner.ports.source import FetchResult

SEED = fixture_json("openalex", "references_seed.json")
DETAILS = fixture_json("openalex", "references_details.json")
DOI = "10.1000/seed"


def source(router: Router, **options) -> OpenAlexSource:
    return OpenAlexSource(
        "openalex",
        {"min_interval_seconds": 0, **options},
        contact_email="test@example.org",
        transport=router.transport,
        today=lambda: date(2026, 11, 11),
    )


def routed(seed=SEED) -> Router:
    def details(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["filter"].removeprefix("openalex:").split("|")
        items = [row for row in DETAILS["results"] if row["id"].split("/")[-1] in ids]
        return httpx.Response(200, json={"meta": {"count": len(items)}, "results": items})

    return Router().reply("/works/doi:", json_body=seed).on("/works?", details)


def test_reference_query_round_trip_is_separate_from_daily_discovery():
    query = ReferenceList(dois=(DOI, "10.1000/another"))
    assert query_from_payload(query.model_dump(mode="json")) == query
    assert query.hash == ReferenceList(dois=tuple(reversed(query.dois))).hash
    assert not any(isinstance(item, ReferenceList) for item in plan_queries((), {}))
    assert FetchResult((), None, (), None, False).references == ()


def test_outgoing_references_keep_old_papers_order_missing_details_and_aliases():
    router = routed()
    result = source(router).fetch(ReferenceList(dois=(DOI,)), "not-a-daily-cursor")
    assert result.next_cursor is None and not result.truncated
    assert [ref.position for ref in result.references] == [0, 1, 2, 3]
    assert all(ref.citing_doi == DOI for ref in result.references)
    assert [record.published_at.year for record in result.records] == [1990, 1980]
    assert result.references[0].cited_title == "Historical reference"
    assert set(result.references[0].cited_aliases) == {
        Alias("doi", "10.1000/old"),
        Alias("arxiv", "1706.03762"),
        Alias("openalex", "W1"),
        Alias("pmid", "123"),
        Alias("pmcid", "PMC456"),
    }
    missing = result.references[2]
    assert missing.cited_title is None and missing.cited_aliases == (Alias("openalex", "W3"),)
    assert json.loads(missing.raw_text) == {
        "referenced_work": SEED["referenced_works"][2],
        "work": None,
    }
    assert any("W3" in warning for warning in result.warnings)
    assert result.references[0].contexts == () and result.references[0].influential is None
    assert (
        result.raw and b"referenced_works" in result.raw and b"Historical reference" in result.raw
    )
    assert router.params(0)["select"] == "id,referenced_works"
    assert router.params(1)["filter"] == "openalex:W1|W2|W3"
    assert all("publication" not in router.params(i).get("filter", "") for i in range(2))
    assert all("cites:" not in str(request.url) for request in router.requests)


def test_reference_details_are_batched_and_limit_counts_raw_entries_not_unique_metadata():
    router = routed()
    result = source(router, per_page=1, max_records=3).fetch(ReferenceList(dois=(DOI,)), None)
    assert len(result.references) == 3 and len(result.records) == 2 and result.truncated
    assert len(router.requests) == 4
    assert [router.params(i)["per_page"] for i in range(1, 4)] == ["1"] * 3
    assert "W3" not in str(router.requests[1].url)
    assert any("上限 3" in warning for warning in result.warnings)


def test_max_records_is_shared_across_seeds_and_no_extra_seed_is_requested():
    router = routed()
    result = source(router, max_records=1).fetch(ReferenceList(dois=(DOI, "10.1000/second")), None)
    assert len(result.references) == 1 and result.truncated
    assert len(router.requests) == 2
    assert "second" not in str(router.requests)


def test_more_than_100_references_use_separate_id_batches():
    seed = {"referenced_works": [f"https://openalex.org/W{i}" for i in range(110)]}

    def details(request: httpx.Request) -> httpx.Response:
        ids = request.url.params["filter"].removeprefix("openalex:").split("|")
        assert len(ids) <= 100
        return httpx.Response(200, json={"results": [{"id": key, "title": key} for key in ids]})

    router = Router().reply("/works/doi:", json_body=seed).on("/works?", details)
    result = source(router).fetch(ReferenceList(dois=(DOI,)), None)
    assert len(result.references) == 110 and len(result.records) == 110
    assert len(router.requests) == 3 and not result.truncated
    assert [router.params(i)["per_page"] for i in (1, 2)] == ["100", "10"]


@pytest.mark.parametrize("status", [404, 410])
def test_missing_seed_is_reported_and_raw_is_retained(status):
    router = Router().reply("/works/doi:", status, json_body={"error": "missing"})
    result = source(router).fetch(ReferenceList(dois=(DOI,)), None)
    assert not result.records and not result.references and result.next_cursor is None
    assert any(DOI in warning for warning in result.warnings)
    assert json.loads(result.raw)["status"] == status


def test_empty_seed_and_empty_query_need_no_detail_requests():
    router = routed({"referenced_works": []})
    result = source(router).fetch(ReferenceList(dois=(DOI,)), None)
    assert not result.references and not result.warnings and len(router.requests) == 1
    empty = Router()
    assert not source(empty).fetch(ReferenceList(dois=()), None).references
    assert empty.requests == []


@pytest.mark.parametrize("body", [{}, {"referenced_works": None}, {"referenced_works": [None]}])
def test_bad_seed_shape_raises_schema_error_and_keeps_http_raw(body):
    adapter = source(routed(body))
    with pytest.raises(SchemaChanged):
        adapter.fetch(ReferenceList(dois=(DOI,)), None)
    assert adapter._http.raw_log()  # 原始响应在解析之前留档。


@pytest.mark.parametrize("body", [{}, {"results": [None]}, {"results": [{"id": "W404"}]}])
def test_bad_detail_shape_raises_schema_error(body):
    router = Router().reply("/works/doi:", json_body=SEED).reply("/works?", json_body=body)
    with pytest.raises(SchemaChanged):
        source(router).fetch(ReferenceList(dois=(DOI,)), None)


def test_missing_title_preserves_reference_aliases_and_raw_without_invalid_metadata():
    details = copy.deepcopy(DETAILS)
    details["results"][0]["title"] = None
    router = Router().reply("/works/doi:", json_body=SEED).reply("/works?", json_body=details)
    result = source(router).fetch(ReferenceList(dois=(DOI,)), None)
    assert len(result.references) == 4 and len(result.records) == 1
    assert result.references[1].cited_title is None
    assert Alias("mag", "200") in result.references[1].cited_aliases
    assert any("无效记录" in warning for warning in result.warnings)
