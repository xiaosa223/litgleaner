"""PubMed 信号源。

efetch.xml 是 2026-11-05 真实 efetch 返回的 5 篇文章（删掉了参考文献、MeSH 等无关节点，
作者列表截到前 4 位）。esearch_error.json 是当天真实收到的“HTTP 200 + ERROR”响应。
esearch.json 按 E-utilities 文档构造，未经真实调用验证：当天 esearch 持续故障（SOLR 不可用）。
"""

from datetime import date

import pytest
from tests.helpers.http import Router, fixture_bytes, fixture_json

from litfactory.adapters.sources.pubmed import PubMedSource
from litfactory.domain.errors import ConfigError, RateLimited, SchemaChanged, TransientError
from litfactory.domain.query import AuthorWatch, CitationWatch, JournalWatch, KeywordSearch

TODAY = date(2026, 11, 5)
SEARCH = fixture_json("pubmed", "esearch.json")
EFETCH = fixture_bytes("pubmed", "efetch.xml")


def source(router: Router, *, api_key: str | None = None, **options) -> PubMedSource:
    return PubMedSource(
        "pubmed",
        options,
        contact_email="contact@example.org",
        api_key=api_key,
        transport=router.transport,
        today=lambda: TODAY,
    )


def routed(search=SEARCH) -> Router:
    return Router().reply("esearch.fcgi", json_body=search).reply("efetch.fcgi", body=EFETCH)


def test_declares_keyword_journal_and_author_queries():
    assert set(source(Router()).capabilities) == {KeywordSearch, JournalWatch, AuthorWatch}
    with pytest.raises(ValueError, match="不支持"):
        source(Router()).fetch(CitationWatch(dois=("10.1/x",)), None)


def test_keyword_search_uses_tiab_boolean_and_entrez_date_window():
    router = routed()
    query = KeywordSearch(terms=("CRISPR", "base editing"), exclude=("plant",))
    result = source(router).fetch(query, None)
    params = router.params(0)
    assert params["term"] == '("CRISPR"[tiab] OR "base editing"[tiab]) NOT ("plant"[tiab])'
    assert (params["datetype"], params["mindate"], params["maxdate"]) == (
        "edat",
        "2026/10/29",
        "2026/11/05",
    )
    assert (params["db"], params["retmode"], params["retmax"]) == ("pubmed", "json", "500")
    assert (params["tool"], params["email"]) == ("litfactory", "contact@example.org")
    fetch = router.params(1)
    assert fetch["id"] == "41509970,41501994,41502991,3920644,32678530"
    assert fetch["retmode"] == "xml"
    assert len(result.records) == 5
    assert result.next_cursor == "2026-11-05"
    assert result.raw and b"esearchresult" in result.raw and b"PubmedArticle" in result.raw


def test_journal_and_author_watch_syntax_with_cursor_overlap():
    router = routed()
    source(router).fetch(JournalWatch(issns=("1533-4406", "0028-4793")), "2026-11-04")
    params = router.params(0)
    assert params["term"] == '("0028-4793"[is] OR "1533-4406"[is])'
    assert params["mindate"] == "2026/11/01"
    router = routed()
    source(router).fetch(AuthorWatch(names=("Horby P", "Doudna JA")), None)
    assert router.params(0)["term"] == '("Horby P"[au] OR "Doudna JA"[au])'


def _by_pmid(result, pmid: str):
    return next(r for r in result.records if r.external_ids.get("pmid") == pmid)


def test_structured_abstract_title_markup_ids_and_article_date():
    result = source(routed()).fetch(KeywordSearch(terms=("x",)), None)
    first = _by_pmid(result, "41509970")
    assert first.title.endswith("Trial: In Vivo and In Vitro Study.")
    assert first.abstract and first.abstract.startswith("AIM: This study aims")
    assert "STUDY DESIGN: This study employed both in vitro" in first.abstract
    assert first.authors[:2] == ("Priyanka Raj", "Shikha Choubey")
    assert (first.venue, first.issn) == (
        "International journal of clinical pediatric dentistry",
        "0974-7052",
    )
    assert first.doi == "10.5005/jp-journals-10005-3269"
    assert dict(first.external_ids) == {"pmid": "41509970", "pmcid": "PMC12777055"}
    assert first.published_at == date(2025, 11, 5)


def test_pub_date_fallbacks_and_collective_authors():
    result = source(routed()).fetch(KeywordSearch(terms=("x",)), None)
    assert _by_pmid(result, "41501994").published_at == date(2026, 1, 9)
    old = _by_pmid(result, "3920644")
    assert old.published_at == date(1985, 3, 13)
    assert old.abstract is None and old.doi is None
    assert dict(old.external_ids) == {"pmid": "3920644"}
    trial = _by_pmid(result, "32678530")
    assert trial.authors[0] == "RECOVERY Collaborative Group"
    assert trial.published_at == date(2020, 7, 17)
    markup = _by_pmid(result, "41502991")
    assert "<" not in markup.title and "PSMA Tracer" in markup.title
    assert markup.abstract and "< 0.1 ug/L" in markup.abstract


def test_efetch_runs_in_batches_and_limit_is_reported():
    search = {"esearchresult": {**SEARCH["esearchresult"], "count": "1200"}}
    router = routed(search)
    result = source(router, fetch_batch=2, max_records=5).fetch(KeywordSearch(terms=("x",)), None)
    assert router.params(0)["retmax"] == "5"
    batches = [router.params(i)["id"] for i in range(1, len(router.requests))]
    assert batches == ["41509970,41501994", "41502991,3920644", "32678530"]
    assert any("上限" in warning for warning in result.warnings)


def test_api_key_is_sent_and_scrubbed_and_speeds_up_default_interval():
    router = routed()
    pubmed = source(router, api_key="secret-key")
    result = pubmed.fetch(KeywordSearch(terms=("x",)), None)
    assert router.params(0)["api_key"] == "secret-key"
    assert result.raw and b"secret-key" not in result.raw
    assert pubmed.min_interval == pytest.approx(0.1)
    assert source(Router()).min_interval == pytest.approx(0.34)
    assert source(Router(), min_interval_seconds=2).min_interval == pytest.approx(2)


def test_empty_search_skips_efetch():
    empty = {"esearchresult": {"count": "0", "retmax": "0", "retstart": "0", "idlist": []}}
    router = routed(empty)
    result = source(router).fetch(KeywordSearch(terms=("x",)), None)
    assert result.records == ()
    assert len(router.requests) == 1


def test_error_field_in_ok_response_is_transient():
    router = routed(fixture_json("pubmed", "esearch_error.json"))
    with pytest.raises(TransientError, match="SOLR"):
        source(router).fetch(KeywordSearch(terms=("x",)), None)


def test_rate_limit_and_schema_errors():
    limited = Router().reply("esearch.fcgi", 429, json_body={"error": "API rate limit exceeded"})
    with pytest.raises(RateLimited):
        source(limited).fetch(KeywordSearch(terms=("x",)), None)
    with pytest.raises(SchemaChanged):
        source(routed({"header": {}})).fetch(KeywordSearch(terms=("x",)), None)
    broken = Router().reply("esearch.fcgi", json_body=SEARCH).reply("efetch.fcgi", body=b"<html>")
    with pytest.raises(SchemaChanged):
        source(broken).fetch(KeywordSearch(terms=("x",)), None)


def test_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        source(Router(), fetch_batch=500)
