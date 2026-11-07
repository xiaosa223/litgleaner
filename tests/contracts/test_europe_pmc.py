"""Europe PMC 信号源和开放获取全文。

记录内容是 2026-11-05 从 Europe PMC article 端点（resultType=core）真实取回的
（MED 41509970、MED 21876761、预印本 PPR1000000 和 PPR1150003，裁掉了机构、MeSH 等字段）。
外层的 search 响应结构（hitCount、nextCursorMark、resultList）按文档构造，
未经真实调用验证：当天 search 端点持续返回 503。
"""

from datetime import date

import httpx
import pytest
from tests.helpers.http import Router, fixture_bytes, fixture_json, public_resolver

from litgleaner.adapters.fulltext.europe_pmc import EuropePmcFullText
from litgleaner.adapters.sources.europe_pmc import EuropePmcSource
from litgleaner.domain.errors import ConfigError, RateLimited, SchemaChanged, TransientError
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import AuthorWatch, CitationWatch, JournalWatch, KeywordSearch
from litgleaner.ports.fulltext import FullTextRequest

TODAY = date(2026, 11, 5)
PAGE1 = fixture_json("europe_pmc", "search_page1.json")
PAGE2 = fixture_json("europe_pmc", "search_page2.json")
LOOKUP = fixture_json("europe_pmc", "lookup_oa.json")
PDF = b"%PDF-1.7\nbody"


def source(router: Router, **options) -> EuropePmcSource:
    return EuropePmcSource(
        "europe_pmc",
        options,
        contact_email="contact@example.org",
        transport=router.transport,
        today=lambda: TODAY,
    )


def paged() -> Router:
    def handler(request: httpx.Request) -> httpx.Response:
        first = request.url.params["cursorMark"] == "*"
        return httpx.Response(200, json=PAGE1 if first else PAGE2)

    return Router().on("/search", handler)


def test_declares_keyword_journal_and_author_queries():
    assert set(source(Router()).capabilities) == {KeywordSearch, JournalWatch, AuthorWatch}
    with pytest.raises(ValueError, match="不支持"):
        source(Router()).fetch(CitationWatch(dois=("10.1/x",)), None)


def test_keyword_search_syntax_window_and_cursor_paging():
    router = paged()
    query = KeywordSearch(terms=("single-cell RNA", "scRNA-seq"), exclude=("plant",))
    result = source(router).fetch(query, None)
    first, second = router.params(0), router.params(1)
    assert first["query"] == (
        '("single-cell RNA" OR "scRNA-seq") NOT ("plant") '
        "AND FIRST_IDATE:[2026-10-29 TO 2026-11-05]"
    )
    assert (first["format"], first["resultType"], first["cursorMark"]) == ("json", "core", "*")
    assert first["pageSize"] == "200"
    assert first["email"] == "contact@example.org"
    assert second["cursorMark"] == PAGE1["nextCursorMark"]
    assert len(router.requests) == 2
    assert len(result.records) == 4
    assert result.next_cursor == "2026-11-05"
    assert result.warnings == ()
    assert result.raw and b"resultList" in result.raw


def test_journal_and_author_syntax():
    router = paged()
    source(router).fetch(JournalWatch(issns=("2041-1723", "0028-0836")), "2026-11-04")
    assert router.params(0)["query"] == (
        '(ISSN:"0028-0836" OR ISSN:"2041-1723") AND FIRST_IDATE:[2026-11-01 TO 2026-11-05]'
    )
    router = paged()
    source(router).fetch(AuthorWatch(names=("Raj P",)), None)
    assert router.params(0)["query"].startswith('(AUTH:"Raj P") AND FIRST_IDATE:')


def _by(result, key: str, value: str):
    return next(r for r in result.records if r.external_ids.get(key) == value)


def test_journal_article_fields_are_mapped():
    result = source(paged()).fetch(KeywordSearch(terms=("x",)), None)
    article = _by(result, "pmid", "41509970")
    assert article.title.endswith("Trial: In Vivo and In Vitro Study.")
    assert article.authors[:2] == ("Priyanka Raj", "Shikha Choubey")
    assert article.venue == "International journal of clinical pediatric dentistry"
    assert article.issn and article.issn in {"0974-7052", "0975-1904"}
    assert article.doi == "10.5005/jp-journals-10005-3269"
    assert dict(article.external_ids) == {"pmid": "41509970", "pmcid": "PMC12777055"}
    assert article.published_at == date(2025, 11, 5)
    assert article.abstract and article.abstract.startswith("Aim This study aims")
    assert "<" not in article.abstract


def test_preprints_get_europepmc_id_and_server_as_venue():
    result = source(paged()).fetch(KeywordSearch(terms=("x",)), None)
    preprint = _by(result, "europepmc", "PPR:PPR1000000")
    assert dict(preprint.external_ids) == {"europepmc": "PPR:PPR1000000"}
    assert preprint.venue == "Research Square"
    assert preprint.doi == "10.21203/rs.3.rs-6330309/v1"
    assert preprint.authors[0] == "Kerrigan Gilbert"
    assert preprint.published_at == date(2025, 4, 4)
    assert preprint.abstract and preprint.abstract.startswith("Identifying loci")


def test_limit_stops_paging_and_warns():
    router = paged()
    result = source(router, page_size=3, max_records=3).fetch(KeywordSearch(terms=("x",)), None)
    assert router.params(0)["pageSize"] == "3"
    assert len(router.requests) == 1
    assert len(result.records) == 3
    assert any("上限" in warning for warning in result.warnings)


def test_rate_limit_schema_and_embedded_errors():
    with pytest.raises(RateLimited):
        source(Router().reply("/search", 429)).fetch(KeywordSearch(terms=("x",)), None)
    with pytest.raises(SchemaChanged):
        source(Router().reply("/search", json_body={"hitCount": 1})).fetch(
            KeywordSearch(terms=("x",)), None
        )
    busy = {"errCode": 503, "errMsg": "Search service is temporarily unavailable."}
    with pytest.raises(TransientError, match="temporarily"):
        source(Router().reply("/search", json_body=busy)).fetch(KeywordSearch(terms=("x",)), None)


def test_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        source(Router(), page_size=5000)


# ---- 开放获取全文 ----


CLOUD = "pmc-oa-opendata.s3.amazonaws.com"
CLOUD_LIST = fixture_bytes("europe_pmc", "pmc_cloud_list.xml")
CLOUD_META = fixture_json("europe_pmc", "pmc_cloud_meta.json")
EMPTY_LIST = (
    b'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult '
    b'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><KeyCount>0</KeyCount></ListBucketResult>'
)


def fulltext(router: Router) -> EuropePmcFullText:
    return EuropePmcFullText(
        {}, contact_email=None, transport=router.transport, resolver=public_resolver
    )


def request(*aliases: Alias) -> FullTextRequest:
    return FullTextRequest("P", "Paper", aliases)


def lookup(**changes) -> dict:
    record = {**LOOKUP["resultList"]["result"][0], **changes}
    return {**LOOKUP, "resultList": {"result": [record]}}


def cloud(router: Router, listing: bytes = CLOUD_LIST, meta: dict = CLOUD_META) -> Router:
    return (
        router.reply(f"{CLOUD}/?", body=listing)
        .reply(f"{CLOUD}/metadata/PMC3158090.1.json", json_body=meta)
        .reply(f"{CLOUD}/PMC3158090.1/PMC3158090.1.pdf", body=PDF)
    )


def test_pmcid_goes_straight_to_pmc_open_access_cloud_copy():
    router = cloud(Router())
    found = fulltext(router).fetch(request(Alias("pmcid", "PMC3158090")))
    assert found is not None
    assert (found.content, found.license_basis, found.provider) == (PDF, "open", "europe_pmc")
    assert router.params(0) == {"list-type": "2", "prefix": "metadata/PMC3158090."}
    assert [r.url.path for r in router.requests[1:]] == [
        "/metadata/PMC3158090.1.json",
        "/PMC3158090.1/PMC3158090.1.pdf",
    ]


def test_without_cloud_copy_falls_back_to_open_pdf_links_from_search():
    router = (
        cloud(Router(), listing=EMPTY_LIST)
        .reply("/search", json_body=lookup())
        .reply("europepmc.org/articles/PMC3158090", 403, body=b"challenge")
        .reply("journals.plos.org", body=PDF)
    )
    found = fulltext(router).fetch(request(Alias("pmcid", "PMC3158090")))
    assert found is not None
    search = next(r for r in router.requests if r.url.path.endswith("/search"))
    assert search.url.params["query"] == 'PMCID:"PMC3158090"'
    hosts = [r.url.host for r in router.requests[2:]]
    assert hosts == ["europepmc.org", "journals.plos.org"]


def test_doi_only_is_looked_up_then_uses_cloud_copy():
    router = cloud(Router().reply("/search", json_body=lookup()))
    found = fulltext(router).fetch(request(Alias("doi", "10.1371/journal.pone.0023673")))
    assert found is not None
    assert router.params(0)["query"] == 'DOI:"10.1371/journal.pone.0023673"'
    assert router.requests[-1].url.path == "/PMC3158090.1/PMC3158090.1.pdf"


def test_cloud_copy_that_is_not_open_access_is_ignored():
    meta = {**CLOUD_META, "is_pmc_openaccess": False, "is_manuscript": True}
    closed = {"hitCount": 0, "resultList": {"result": []}}
    router = cloud(Router(), meta=meta).reply("/search", json_body=closed)
    assert fulltext(router).fetch(request(Alias("pmcid", "PMC3158090"))) is None
    assert all(not r.url.path.endswith(".pdf") for r in router.requests)


def test_closed_unknown_or_unidentified_papers_return_none():
    closed = (
        Router()
        .reply("/search", json_body=lookup(isOpenAccess="N", license=None))
        .reply("europepmc.org", body=PDF)
        .reply(CLOUD, body=PDF)
    )
    assert fulltext(closed).fetch(request(Alias("doi", "10.1/closed"))) is None
    assert len(closed.requests) == 1
    empty = {"hitCount": 0, "resultList": {"result": []}}
    none_found = Router().reply("/search", json_body=empty)
    assert fulltext(none_found).fetch(request(Alias("doi", "10.1/none"))) is None
    nothing = Router()
    assert fulltext(nothing).fetch(request(Alias("arxiv", "2101.00001"))) is None
    assert nothing.requests == []


def test_non_pdf_responses_are_skipped():
    router = (
        Router()
        .reply(f"{CLOUD}/?", body=EMPTY_LIST)
        .reply("/search", json_body=lookup())
        .reply("http", body=b"<html></html>")
    )
    assert fulltext(router).fetch(request(Alias("pmcid", "PMC3158090"))) is None


def test_creative_commons_preprint_counts_as_open():
    record = lookup(isOpenAccess="N", license="cc by", pmcid=None, inEPMC="N")
    router = Router().reply("/search", json_body=record).reply("journals.plos.org", body=PDF)
    found = fulltext(router).fetch(request(Alias("doi", "10.21203/rs.3.rs-6330309/v1")))
    assert found is not None
    assert found.license_basis == "open"
