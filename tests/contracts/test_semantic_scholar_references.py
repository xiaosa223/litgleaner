"""S2 官方 references 响应结构的离线合同，不请求线上服务。"""

import copy
import json
from datetime import date

import httpx
import pytest
from tests.helpers.http import Router, fixture_json

from litgleaner.adapters.sources.semantic_scholar import SemanticScholarSource
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import ReferenceList

PAGE1 = fixture_json("semantic_scholar", "references_page1.json")
PAGE2 = fixture_json("semantic_scholar", "references_page2.json")
DOI = "10.1000/seed"


def source(router: Router, **options) -> SemanticScholarSource:
    return SemanticScholarSource(
        "semantic_scholar",
        {"min_interval_seconds": 0, **options},
        contact_email=None,
        transport=router.transport,
        today=lambda: date(2026, 11, 11),
    )


def routed() -> Router:
    def page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=PAGE1 if request.url.params["offset"] == "0" else PAGE2)

    return Router().on("/references", page)


def test_reference_list_is_outgoing_unwindowed_and_preserves_all_evidence():
    router = routed()
    result = source(router, page_size=2).fetch(ReferenceList(dois=(DOI,)), "ignored-cursor")
    assert result.next_cursor is None and not result.truncated
    assert [ref.position for ref in result.references] == [0, 1, 2, 3]
    assert len(result.records) == 2 and result.records[0].published_at == date(1990, 2, 3)
    assert result.records[1].published_at == date(1970, 1, 1)
    first, deleted, unidentified, repeated = result.references
    assert first.citing_doi == DOI and first.cited_title == "Historical reference"
    assert first.contexts == tuple(PAGE1["data"][0]["contexts"])
    assert first.intents == ("background", "methodology") and first.influential is True
    assert set(first.cited_aliases) == {
        Alias("doi", "10.1000/old"),
        Alias("arxiv", "1706.03762"),
        Alias("s2", "paper-old"),
        Alias("pmid", "123"),
        Alias("pmcid", "PMC456"),
        Alias("corpusid", "789"),
    }
    assert deleted.cited_title is None and deleted.cited_aliases == ()
    assert deleted.influential is False and deleted.contexts
    assert json.loads(deleted.raw_text)["citedPaper"] is None
    assert unidentified.cited_aliases == () and unidentified.influential is None
    assert repeated.cited_title == first.cited_title and repeated.position == 3
    assert all(ref.citing_doi == DOI for ref in result.references)
    assert [router.params(i)["offset"] for i in range(2)] == ["0", "2"]
    assert all(
        request.url.path.endswith(f"/paper/DOI:{DOI}/references") for request in router.requests
    )
    assert all("publicationDateOrYear" not in router.params(i) for i in range(2))
    assert "citedPaper." not in router.params(0)["fields"]
    assert {"contexts", "intents", "isInfluential", "externalIds"} <= set(
        router.params(0)["fields"].split(",")
    )
    assert len(result.raw.splitlines()) == 2 and b"citedPaper" in result.raw


@pytest.mark.parametrize(("limit", "calls", "last_limit"), [(1, 1, "1"), (2, 1, "2"), (3, 2, "1")])
def test_limit_counts_deleted_and_duplicate_targets_and_bounds_requests(limit, calls, last_limit):
    router = routed()
    result = source(router, max_records=limit, page_size=2).fetch(ReferenceList(dois=(DOI,)), None)
    assert len(result.references) == limit and result.truncated
    assert len(router.requests) == calls and router.params()["limit"] == last_limit
    assert any(f"上限 {limit}" in warning for warning in result.warnings)


def test_exact_limit_without_more_pages_is_complete():
    router = routed()
    result = source(router, max_records=4, page_size=2).fetch(ReferenceList(dois=(DOI,)), None)
    assert len(result.references) == 4 and not result.truncated


def test_limit_is_shared_across_seeds_and_offsets_restart_per_seed():
    router = Router().reply("/references", json_body={"offset": 0, "data": PAGE1["data"][:1]})
    result = source(router, max_records=2).fetch(
        ReferenceList(dois=(DOI, "10.1000/second", "10.1000/third")), None
    )
    assert [ref.citing_doi for ref in result.references] == [DOI, "10.1000/second"]
    assert [ref.position for ref in result.references] == [0, 0]
    assert len(router.requests) == 2 and result.truncated


@pytest.mark.parametrize("status", [404, 410])
def test_missing_seed_is_warned_and_raw_retained(status):
    router = Router().reply("/references", status, json_body={"error": "missing"})
    result = source(router).fetch(ReferenceList(dois=(DOI,)), None)
    assert not result.references and not result.records and result.next_cursor is None
    assert any(DOI in warning for warning in result.warnings)
    assert json.loads(result.raw)["status"] == status


def test_empty_list_and_empty_query_finish_without_an_extra_page():
    router = Router().reply("/references", json_body={"offset": 0, "data": []})
    result = source(router).fetch(ReferenceList(dois=(DOI,)), None)
    assert not result.references and not result.truncated and len(router.requests) == 1
    empty = Router()
    assert not source(empty).fetch(ReferenceList(dois=()), None).references
    assert not empty.requests


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"data": "oops"},
        {"data": [None]},
        {"data": [{}]},
        {"data": [{"citingPaper": {"title": "Wrong direction"}}]},
        {"data": [{"citedPaper": "oops"}]},
        {"offset": 1, "data": []},
        {"data": [], "next": 0},
        {"data": [], "next": True},
        {"data": [], "next": 1},
    ],
)
def test_bad_structure_direction_or_pagination_is_schema_error_and_raw_survives(body):
    adapter = source(Router().reply("/references", json_body=body))
    with pytest.raises(SchemaChanged):
        adapter.fetch(ReferenceList(dois=(DOI,)), None)
    assert adapter._http.raw_log()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("contexts", "invented"),
        ("contexts", [123]),
        ("intents", {}),
        ("isInfluential", "yes"),
    ],
)
def test_bad_reference_annotations_are_not_coerced_into_facts(key, value):
    item = copy.deepcopy(PAGE1["data"][0])
    item[key] = value
    router = Router().reply("/references", json_body={"data": [item]})
    with pytest.raises(SchemaChanged):
        source(router).fetch(ReferenceList(dois=(DOI,)), None)


@pytest.mark.parametrize(
    "changes",
    [
        {"externalIds": []},
        {"externalIds": {"DOI": 123}},
        {"paperId": 123},
        {"title": ["bad"]},
    ],
)
def test_bad_target_metadata_is_schema_error(changes):
    item = copy.deepcopy(PAGE1["data"][0])
    item["citedPaper"].update(changes)
    router = Router().reply("/references", json_body={"data": [item]})
    with pytest.raises(SchemaChanged):
        source(router).fetch(ReferenceList(dois=(DOI,)), None)
