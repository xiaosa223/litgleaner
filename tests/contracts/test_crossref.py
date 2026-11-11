"""Crossref 信号源和文本挖掘全文。录制数据来自 2026-11-05 的真实调用，邮箱已替换。"""

from datetime import date

import pytest
from tests.helpers.http import Router, fixture_bytes, fixture_json, public_resolver

from litgleaner.adapters.fulltext.crossref_tdm import CrossrefTdmFullText
from litgleaner.adapters.sources.crossref import CrossrefSource
from litgleaner.domain.errors import ConfigError, RateLimited, SchemaChanged
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import AuthorWatch, IdentifierLookup, JournalWatch, KeywordSearch
from litgleaner.ports.fulltext import FullTextRequest

TODAY = date(2026, 11, 5)
KEYWORD = fixture_json("crossref", "keyword.json")
JOURNAL = fixture_json("crossref", "journal.json")
PDF = b"%PDF-1.7\nbody"


def source(router: Router, **options) -> CrossrefSource:
    return CrossrefSource(
        "crossref",
        options,
        contact_email="contact@example.org",
        transport=router.transport,
        today=lambda: TODAY,
    )


def test_declares_journal_and_keyword_queries_only():
    capabilities = source(Router()).capabilities
    assert {JournalWatch, KeywordSearch, IdentifierLookup} == set(capabilities)
    with pytest.raises(ValueError, match="不支持"):
        source(Router()).fetch(AuthorWatch(names=("A",)), None)


def test_keyword_search_runs_per_term_and_filters_locally():
    router = Router().reply("/works", json_body=KEYWORD)
    query = KeywordSearch(terms=("graph neural network",), exclude=("misconduct",))
    result = source(router).fetch(query, None)
    params = router.params(0)
    assert params["query.bibliographic"] == "graph neural network"
    assert params["filter"] == "from-created-date:2026-10-29,until-created-date:2026-11-05"
    assert params["mailto"] == "contact@example.org"
    titles = [record.title for record in result.records]
    assert "Graph neural network approach for inferring neuronal connectivity" in titles
    assert not any("Misconduct" in title for title in titles)
    assert result.next_cursor == "2026-11-05"
    assert result.raw and b"Graph neural" in result.raw


def test_record_fields_are_mapped():
    router = Router().reply("/works", json_body=KEYWORD)
    result = source(router).fetch(KeywordSearch(terms=("graph neural network",)), None)
    first = next(r for r in result.records if r.doi == "10.64898/2026.10.28.762597")
    assert first.authors[:2] == ("Archit Gupta", "Curtis L Neveu")
    assert first.published_at == date(2026, 11, 3)
    assert first.abstract and first.abstract.startswith("Understanding the connectivity")
    assert "<" not in first.abstract


def test_journal_watch_pages_with_deep_cursor_and_stops_at_limit():
    router = Router().reply("/works", json_body=JOURNAL)
    result = source(router, rows=3, max_records=6).fetch(
        JournalWatch(issns=("1476-4687", "0028-0836")), "2026-11-04"
    )
    first, second = router.params(0), router.params(1)
    assert first["filter"] == (
        "issn:0028-0836,issn:1476-4687,from-created-date:2026-11-01,until-created-date:2026-11-05"
    )
    assert first["cursor"] == "*"
    assert second["cursor"] == JOURNAL["message"]["next-cursor"]
    assert len(router.requests) == 2
    assert any("上限" in warning for warning in result.warnings)
    assert all(record.issn for record in result.records)


def test_rate_limit_and_schema_errors():
    with pytest.raises(RateLimited):
        source(Router().reply("/works", 429)).fetch(KeywordSearch(terms=("x",)), None)
    broken = Router().reply("/works", json_body={"status": "ok", "message": {}})
    with pytest.raises(SchemaChanged):
        source(broken).fetch(KeywordSearch(terms=("x",)), None)


def test_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        source(Router(), rows=0)


# ---- 文本挖掘全文 ----


def tdm(router: Router, **options) -> CrossrefTdmFullText:
    return CrossrefTdmFullText(
        options,
        contact_email=None,
        transport=router.transport,
        resolver=public_resolver,
    )


def request(doi: str | None = "10.1038/s41586-021-03819-2") -> FullTextRequest:
    aliases = (Alias("doi", doi),) if doi else (Alias("arxiv", "2101.00001"),)
    return FullTextRequest("P", "Paper", aliases)


def test_tdm_downloads_pdf_link_and_marks_open_license():
    router = (
        Router()
        .reply("api.crossref.org/works/10.1038", body=fixture_bytes("crossref", "work_nature.json"))
        .reply("www.nature.com/articles/s41586-021-03819-2.pdf", body=PDF)
    )
    found = tdm(router).fetch(request())
    assert found is not None
    assert (found.content, found.license_basis, found.provider) == (PDF, "open", "crossref_tdm")


def test_tdm_without_open_license_is_marked_subscribed():
    work = fixture_json("crossref", "work_nature.json")
    work["message"]["license"] = [{"URL": "https://www.example.com/tdm/userlicense/1.0/"}]
    router = (
        Router().reply("api.crossref.org/works/", json_body=work).reply("www.nature.com", body=PDF)
    )
    found = tdm(router).fetch(request())
    assert found is not None
    assert found.license_basis == "subscribed"


def test_tdm_skips_non_pdf_links_and_missing_doi():
    router = Router().reply(
        "api.crossref.org/works/", body=fixture_bytes("crossref", "work_elsevier.json")
    )
    assert tdm(router).fetch(request("10.1016/j.patcog.2023.109437")) is None
    assert all("elsevier" not in str(r.url) for r in router.requests)
    assert tdm(Router()).fetch(request(None)) is None


def test_tdm_unknown_doi_is_not_an_error():
    assert tdm(Router()).fetch(request("10.9999/missing")) is None


# ---- 按标识符查（添加论文，第 12.8 节） ----

WORK = fixture_json("crossref", "work_nature.json")


def test_identifier_lookup_fetches_one_work_by_doi():
    router = Router().reply("/works/10.1038/s41586-021-03819-2", json_body=WORK)
    result = source(router).fetch(IdentifierLookup(doi="10.1038/s41586-021-03819-2"), None)
    (record,) = result.records
    assert record.doi == "10.1038/s41586-021-03819-2"
    assert record.title.startswith("Highly accurate protein structure prediction")
    assert result.next_cursor is None  # 一次性查询，不推进游标


def test_identifier_lookup_unknown_doi_returns_nothing():
    router = Router().reply("/works/10.9999/nope", json_body={}, status=404)
    assert source(router).fetch(IdentifierLookup(doi="10.9999/nope"), None).records == ()


def test_identifier_lookup_without_doi_returns_nothing():
    assert source(Router()).fetch(IdentifierLookup(arxiv_id="1706.03762"), None).records == ()
