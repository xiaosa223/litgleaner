"""日常引文证据只来自实际采纳的引用者和本次请求明确命中的种子。"""

import json
from datetime import date

import httpx
import pytest
from tests.helpers.http import Router

from litgleaner.adapters.sources.openalex import OpenAlexSource
from litgleaner.adapters.sources.semantic_scholar import SemanticScholarSource
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import CitationWatch, KeywordSearch

TODAY = date(2026, 11, 5)
A, B = "10.1234/a", "10.1234/b"


def source(kind, router, **options):
    cls = OpenAlexSource if kind == "openalex" else SemanticScholarSource
    return cls(
        kind,
        {"min_interval_seconds": 0, **options},
        contact_email=None,
        transport=router.transport,
        today=lambda: TODAY,
    )


def work(number, refs, **extra):
    return {
        "id": f"https://openalex.org/W{number}",
        "doi": f"https://doi.org/10.1234/{number}",
        "title": f"Paper {number}",
        "publication_date": "2026-11-01",
        "referenced_works": refs,
        **extra,
    }


def paper(number, **extra):
    return {
        "paperId": f"s2-{number}",
        "externalIds": {"DOI": f"10.1234/{number}"},
        "title": f"Paper {number}",
        "publicationDate": "2026-11-01",
        **extra,
    }


def oa_router(items, seeds=None):
    seeds = seeds or {A: "W1", B: "W2"}
    router = Router().on(
        "/works/doi:",
        lambda req: httpx.Response(
            200,
            json={"id": f"https://openalex.org/{seeds[req.url.path.removeprefix('/works/doi:')]}"},
        ),
    )
    return router.reply("/works", json_body={"meta": {"count": len(items)}, "results": items})


def endpoints(result):
    return {
        (
            next(a.value for a in edge.citing_aliases if a.scheme in {"doi", "openalex", "s2"}),
            next(a.value for a in edge.cited_aliases if a.scheme == "doi"),
        )
        for edge in result.citations
    }


def test_openalex_or_query_preserves_precise_endpoints_and_existing_requests():
    items = [work(10, ["W1"]), work(20, ["https://openalex.org/W2"]), work(30, ["W1", "W2"])]
    router = oa_router(items)
    result = source("openalex", router).fetch(CitationWatch(dois=(A, B)), None)
    assert endpoints(result) == {
        ("10.1234/10", A),
        ("10.1234/20", B),
        ("10.1234/30", A),
        ("10.1234/30", B),
    }
    assert len(result.citations) == 4
    assert len(router.requests) == 3
    assert router.params(2)["filter"].startswith("cites:W1|W2,")
    assert "referenced_works" in router.params(2)["select"].split(",")
    assert result.citations[0].raw_text == json.dumps(items[0], ensure_ascii=False, sort_keys=True)


@pytest.mark.parametrize("bad", [None, "W1", ["W1", 4], {"W1": True}, []])
def test_openalex_multi_seed_never_guesses_from_missing_or_invalid_references(bad):
    item = work(10, bad)
    if bad is None:
        del item["referenced_works"]
    result = source("openalex", oa_router([item])).fetch(CitationWatch(dois=(A, B)), None)
    assert len(result.records) == 1
    assert result.citations == ()


def test_openalex_single_seed_filter_proves_missing_field_but_not_contradictory_field():
    missing = work(10, [])
    del missing["referenced_works"]
    items = [missing, work(20, ["W2"]), work(30, None), work(40, "W1")]
    result = source("openalex", oa_router(items)).fetch(CitationWatch(dois=(A,)), None)
    assert endpoints(result) == {("10.1234/10", A)}


def test_openalex_only_in_window_identified_accepted_and_raw_quota_records_get_edges():
    items = [
        work(10, ["W1"], publication_date="2020-01-01"),
        work(20, ["W1"], title=None),
        work(30, ["W1"], id=None, doi=None),
        work(40, ["W1"], doi=None),
        work(50, ["W1"]),
    ]
    router = oa_router(items)
    result = source("openalex", router, max_records=4).fetch(CitationWatch(dois=(A,)), None)
    assert endpoints(result) == {("W40", A)}
    assert len(result.records) == 3
    assert result.truncated
    assert len(router.requests) == 2


def test_openalex_same_wid_retains_doi_aliases_without_duplicate_relations():
    router = oa_router([work(10, ["W1"])], {A: "W1", B: "W1"})
    result = source("openalex", router).fetch(CitationWatch(dois=(A, B, A)), None)
    assert len(result.citations) == 1
    assert set(result.citations[0].cited_aliases) == {
        Alias("doi", A),
        Alias("doi", B),
        Alias("openalex", "W1"),
    }
    assert router.params(-1)["filter"].startswith("cites:W1,")


def test_openalex_or_groups_only_match_their_own_resolved_seeds():
    seeds = {f"10.1234/seed-{i}": f"W{i}" for i in range(1, 102)}
    # 同一 work 在两批出现，第一批不能根据跨批参考文献把 W101 归属给第一批。
    router = oa_router([work(200, ["W1", "W101"])], seeds)
    result = source("openalex", router).fetch(CitationWatch(dois=tuple(seeds)), None)
    assert len(result.records) == 1
    assert len(result.citations) == 2
    assert endpoints(result) == {
        ("10.1234/200", "10.1234/seed-1"),
        ("10.1234/200", "10.1234/seed-101"),
    }
    assert [edge.source_position for edge in result.citations] == [0, 1]
    assert len(router.requests) == 103


@pytest.mark.parametrize("keyed", [False, True])
def test_openalex_repeated_pages_deduplicate_evidence_and_stop_at_raw_limit(keyed):
    router = oa_router([])
    router.rules.pop()
    router.reply(
        "/works",
        json_body={"meta": {"count": 3, "next_cursor": "NEXT"}, "results": [work(10, ["W1"])]},
    )
    adapter = source("openalex", router, per_page=1, max_records=2)
    adapter._keyed = keyed
    result = adapter.fetch(CitationWatch(dois=(A,)), None)
    assert len(result.records) == len(result.citations) == 1
    assert result.truncated
    assert len(router.requests) == 3
    key = "cursor" if keyed else "page"
    assert router.params(1)[key] == ("*" if keyed else "1")
    assert router.params(2)[key] == ("NEXT" if keyed else "2")


def test_s2_seed_specific_metadata_raw_and_no_doi_are_preserved():
    entry = {
        "contexts": ["证据句"],
        "intents": ["Background"],
        "isInfluential": True,
        "citingPaper": paper(10),
    }
    no_doi = {"citingPaper": paper(20, externalIds={})}
    router = Router().reply(f"DOI:{A}/citations", json_body={"data": [entry, no_doi]})
    router.reply(f"DOI:{B}/citations", json_body={"data": [entry]})
    result = source("s2", router).fetch(CitationWatch(dois=(B, A)), None)
    assert endpoints(result) == {("10.1234/10", A), ("s2-20", A), ("10.1234/10", B)}
    assert len(result.records) == 2
    first = result.citations[0]
    assert (first.contexts, first.intents, first.influential) == (
        ("证据句",),
        ("Background",),
        True,
    )
    assert first.raw_text == json.dumps(entry, ensure_ascii=False, sort_keys=True)
    assert Alias("s2", "s2-10") in first.citing_aliases
    assert {"contexts", "intents", "isInfluential"} <= set(router.params(0)["fields"].split(","))


@pytest.mark.parametrize("bad", ["abc", ["ok", 1], 3, {"x": "y"}])
def test_s2_invalid_outer_metadata_types_become_empty_and_not_fabricated(bad):
    entry = {"contexts": bad, "intents": bad, "isInfluential": bad, "citingPaper": paper(10)}
    router = Router().reply("/citations", json_body={"data": [entry]})
    result = source("s2", router).fetch(CitationWatch(dois=(A,)), None)
    edge = result.citations[0]
    assert (edge.contexts, edge.intents, edge.influential) == ((), (), None)


def test_s2_only_window_valid_identified_and_raw_quota_entries_produce_edges():
    entries = [
        {"citingPaper": paper(10, publicationDate="2020-01-01")},
        {"citingPaper": paper(20, title="")},
        {"citingPaper": paper(30, paperId=None, externalIds={})},
        {"citingPaper": paper(40)},
        {"citingPaper": paper(50)},
    ]
    router = Router().reply("/citations", json_body={"data": entries, "next": 5})
    result = source("s2", router, max_records=4).fetch(CitationWatch(dois=(A, B)), None)
    assert endpoints(result) == {("10.1234/40", A)}
    assert len(result.records) == 2
    assert result.truncated
    assert len(router.requests) == 1


def test_s2_repeated_pages_and_cross_seed_edges_are_exact():
    entry = {"citingPaper": paper(10)}

    def handler(request):
        next_page = request.url.params.get("offset") != "0"
        body = {"data": [entry], "offset": 1 if next_page else 0}
        if not next_page:
            body["next"] = 1
        return httpx.Response(200, json=body)

    router = Router().on("/citations", handler)
    result = source("s2", router).fetch(CitationWatch(dois=(A, B)), None)
    assert len(result.records) == 1
    assert len(result.citations) == 2
    assert endpoints(result) == {("10.1234/10", A), ("10.1234/10", B)}
    assert len(router.requests) == 4


@pytest.mark.parametrize("kind", ["openalex", "s2"])
def test_prior_accepted_identity_cannot_make_rejected_or_over_quota_evidence_valid(kind):
    if kind == "openalex":
        items = [
            work(10, ["W1"]),
            work(10, ["W2"], title=""),
            work(10, ["W2"], publication_date="2020-01-01"),
            work(10, ["W2"]),
        ]
        router = oa_router(items)
    else:
        router = Router().reply(
            f"DOI:{A}/citations", json_body={"data": [{"citingPaper": paper(10)}]}
        )
        router.reply(
            f"DOI:{B}/citations",
            json_body={
                "data": [
                    {"citingPaper": paper(10, title="")},
                    {"citingPaper": paper(10, publicationDate="2020-01-01")},
                    {"citingPaper": paper(10)},
                ]
            },
        )
    result = source(kind, router, max_records=3).fetch(CitationWatch(dois=(A, B)), None)
    assert len(result.records) == 1
    assert endpoints(result) == {("10.1234/10", A)}
    assert result.truncated


@pytest.mark.parametrize("bad_id", [True, ["bad"], {"id": "bad"}, "   "])
@pytest.mark.parametrize("valid_doi", [False, True])
def test_s2_malformed_ids_never_link_different_seed_citers_through_stringification(
    bad_id, valid_doi
):
    router = Router()
    for doi, number in [(A, 10), (B, 20)]:
        ids = {"DOI": f"10.1234/{number}"} if valid_doi else {}
        entry = {"citingPaper": paper(number, paperId=bad_id, externalIds=ids)}
        router.reply(f"DOI:{doi}/citations", json_body={"data": [entry]})
    result = source("s2", router).fetch(CitationWatch(dois=(A, B)), None)
    assert result.records  # 保留原 builder 的条数与去重，候选和证据同步净化标识符。
    assert endpoints(result) == ({("10.1234/10", A), ("10.1234/20", B)} if valid_doi else set())
    assert all(a.scheme != "s2" for edge in result.citations for a in edge.citing_aliases)


def test_s2_pmcid_bool_stringified_by_record_mapping_cannot_become_evidence():
    entry = {"citingPaper": paper(10, paperId=None, externalIds={"PubMedCentral": True})}
    router = Router().reply("/citations", json_body={"data": [entry]})
    result = source("s2", router).fetch(CitationWatch(dois=(A, B)), None)
    assert len(result.records) == 1
    assert result.records[0].external_ids == {}
    assert result.citations == ()


@pytest.mark.parametrize("kind", ["openalex", "s2"])
def test_non_citation_queries_have_no_edges_or_citation_only_requested_fields(kind):
    body = (
        {"meta": {"count": 1}, "results": [work(10, ["W1"])]}
        if kind == "openalex"
        else {"data": [paper(10)]}
    )
    router = Router().reply(
        "/works" if kind == "openalex" else "/paper/search/bulk", json_body=body
    )
    result = source(kind, router).fetch(KeywordSearch(terms=("x",)), None)
    assert len(result.records) == 1
    assert result.citations == ()
    fields = router.params(0).get("select", router.params(0).get("fields", ""))
    assert not {"contexts", "intents", "isInfluential", "referenced_works"} & set(fields.split(","))
