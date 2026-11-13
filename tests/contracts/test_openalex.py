"""OpenAlex 信号源和全文。录制数据来自 2026-11-05 的真实调用（无 key），邮箱已替换，
条目和字段已裁剪。keyword.json 是带布尔检索的真实响应；rate_limited.json 是匿名检索被限流时的
真实 429 响应体。"""

import copy
from datetime import date

import httpx
import pytest
from tests.helpers.http import Router, fixture_json, public_resolver

from litgleaner.adapters.fulltext.openalex import OpenAlexFullText
from litgleaner.adapters.sources.openalex import OpenAlexSource
from litgleaner.domain.errors import ConfigError, RateLimited, SchemaChanged
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import (
    AuthorWatch,
    CitationWatch,
    HistoricalCitations,
    HistoricalSearch,
    InboxDrain,
    JournalWatch,
    KeywordSearch,
    ReferenceList,
    TermCount,
)
from litgleaner.ports.fulltext import FullTextRequest

TODAY = date(2026, 11, 5)
KEYWORD = fixture_json("openalex", "keyword.json")
JOURNAL = fixture_json("openalex", "journal.json")
CITING = fixture_json("openalex", "citing.json")
SEED = fixture_json("openalex", "seed.json")
PDF = b"%PDF-1.7\nbody"
KEY = "test-openalex-key-123"


def source(router: Router, api_key: str | None = None, **options) -> OpenAlexSource:
    return OpenAlexSource(
        "openalex",
        options,
        contact_email="contact@example.org",
        api_key=api_key,
        transport=router.transport,
        today=lambda: TODAY,
    )


def test_declares_query_types_and_rejects_inbox():
    capabilities = source(Router()).capabilities
    assert set(capabilities) == {
        JournalWatch,
        KeywordSearch,
        CitationWatch,
        AuthorWatch,
        ReferenceList,
        HistoricalSearch,
        HistoricalCitations,
        TermCount,
    }
    with pytest.raises(ValueError, match="不支持"):
        source(Router()).fetch(InboxDrain(), None)


def test_keyword_search_uses_boolean_syntax_and_publication_window():
    router = Router().reply("/works", json_body=KEYWORD)
    query = KeywordSearch(terms=("graph neural network",), exclude=("survey",))
    result = source(router).fetch(query, None)
    params = router.params(0)
    assert params["search.title_abstract_keywords"] == '("graph neural network") NOT ("survey")'
    assert params["filter"] == "from_publication_date:2026-10-29,to_publication_date:2026-11-05"
    assert params["sort"] == "publication_date:desc"
    assert params["per_page"] == "100"
    assert params["page"] == "1"
    assert "abstract_inverted_index" in params["select"].split(",")
    assert params["mailto"] == "contact@example.org"
    assert "cursor" not in params
    assert result.records[0].title.startswith("FinMamba")
    assert result.next_cursor == "2026-11-05"
    assert result.raw and b"FinMamba" in result.raw


def test_record_fields_are_mapped_from_openalex_work():
    router = Router().reply("cites%3A", json_body=CITING).reply("/works/doi:", json_body=SEED)
    result = source(router).fetch(CitationWatch(dois=("10.1038/nature14539",)), None)
    first = next(r for r in result.records if r.doi == "10.1038/s41591-026-04655-5")
    assert first.title.startswith("A clinical multimodal vision language foundation model")
    assert first.authors == ("Yang Bai", "Haoran Cheng", "Yang Zhou")
    assert first.published_at == date(2026, 10, 29)
    assert (first.venue, first.issn) == ("Nature Medicine", "1078-8956")
    assert first.arxiv_id == "2509.24231"
    assert dict(first.external_ids) == {"openalex": "W7172250885", "pmid": "42957777"}
    assert first.abstract and first.abstract.startswith("Despite their considerable promise")
    plos = next(r for r in result.records if r.doi == "10.1371/journal.pone.0000308")
    assert plos.external_ids["pmcid"] == "PMC1817752"
    assert plos.arxiv_id is None
    no_pmid = next(r for r in result.records if r.doi == "10.1098/rsos.252099")
    assert "pmid" not in no_pmid.external_ids


def test_citation_watch_resolves_seed_dois_then_filters_by_cites():
    router = Router().reply("cites%3A", json_body=CITING).reply("/works/doi:", json_body=SEED)
    source(router).fetch(CitationWatch(dois=("10.1038/nature14539",)), None)
    lookup, _listing = router.requests
    assert lookup.url.path == "/works/doi:10.1038/nature14539"
    assert router.params(1)["filter"] == (
        "cites:W2919115771,from_publication_date:2026-10-29,to_publication_date:2026-11-05"
    )


def test_unknown_seed_doi_is_reported_and_skipped():
    router = Router()  # 未路由的请求返回 404
    result = source(router).fetch(CitationWatch(dois=("10.9999/missing",)), None)
    assert result.records == ()
    assert len(router.requests) == 1
    assert any("10.9999/missing" in warning for warning in result.warnings)


def test_author_watch_uses_clear_first_candidate():
    router = (
        Router()
        .reply("/authors", json_body=fixture_json("openalex", "authors_lecun.json"))
        .reply("/works", json_body=JOURNAL)
    )
    result = source(router).fetch(AuthorWatch(names=("Yann LeCun",)), None)
    assert router.params(0)["search"] == "Yann LeCun"
    assert router.params(1)["filter"].startswith("authorships.author.id:A5001226970,")
    assert result.records


def test_ambiguous_author_is_skipped_with_warning():
    router = Router().reply("/authors", json_body=fixture_json("openalex", "authors_wei_wang.json"))
    result = source(router).fetch(AuthorWatch(names=("Wei Wang",)), None)
    assert result.records == ()
    assert len(router.requests) == 1
    assert any("Wei Wang" in warning for warning in result.warnings)


def test_author_with_equally_strong_namesake_is_ambiguous():
    authors = fixture_json("openalex", "authors_lecun.json")
    namesake = copy.deepcopy(authors["results"][0])
    namesake.update(id="https://openalex.org/A1", works_count=400)
    authors["results"].insert(1, namesake)
    router = Router().reply("/authors", json_body=authors)
    result = source(router).fetch(AuthorWatch(names=("Yann LeCun",)), None)
    assert len(router.requests) == 1
    assert any("歧义" in warning for warning in result.warnings)


def test_journal_watch_pages_without_key_and_stops_at_limit():
    page = copy.deepcopy(JOURNAL)
    page["meta"]["count"] = 1000
    router = Router().reply("/works", json_body=page)
    result = source(router, per_page=3, max_records=6).fetch(
        JournalWatch(issns=("1476-4687", "0028-0836")), "2026-11-04"
    )
    first, second = router.params(0), router.params(1)
    assert first["filter"] == (
        "primary_location.source.issn:0028-0836|1476-4687,"
        "from_publication_date:2026-09-05,to_publication_date:2026-11-05"
    )
    assert (first["page"], second["page"]) == ("1", "2")
    assert len(router.requests) == 2
    assert any("上限" in warning for warning in result.warnings)
    assert all(record.issn == "0028-0836" for record in result.records)


def test_journal_watch_stops_when_all_results_are_read():
    router = Router().reply("/works", json_body=JOURNAL)
    result = source(router).fetch(JournalWatch(issns=("0028-0836",)), None)
    assert len(router.requests) == 1
    assert not any("上限" in warning for warning in result.warnings)


def test_many_issns_are_split_into_batches_of_100():
    router = Router().reply("/works", json_body=JOURNAL)
    issns = tuple(f"{n:04d}-0000" for n in range(150))
    source(router).fetch(JournalWatch(issns=issns), None)
    assert len(router.requests) == 2
    assert router.params(0)["filter"].count("|") == 99


def test_api_key_enables_cursor_paging_and_stays_out_of_logs():
    first = copy.deepcopy(JOURNAL)
    first["meta"]["next_cursor"] = "NEXT"
    last = copy.deepcopy(JOURNAL)
    last["meta"]["next_cursor"] = None
    pages = iter([first, last])
    router = Router().on("/works", lambda _: httpx.Response(200, json=next(pages)))
    result = source(router, api_key=KEY).fetch(JournalWatch(issns=("0028-0836",)), None)
    assert [router.params(i)["cursor"] for i in range(2)] == ["*", "NEXT"]
    assert router.requests[0].headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in str(router.requests[0].url)
    assert result.raw and KEY.encode() not in result.raw


def test_rate_limit_and_schema_errors():
    limited = Router().reply(
        "/works",
        429,
        json_body=fixture_json("openalex", "rate_limited.json"),
        headers={"Retry-After": "12"},
    )
    with pytest.raises(RateLimited) as error:
        source(limited).fetch(KeywordSearch(terms=("x",)), None)
    assert error.value.retry_after_seconds == 12
    broken = Router().reply("/works", json_body={"meta": {}, "results": "oops"})
    with pytest.raises(SchemaChanged):
        source(broken).fetch(KeywordSearch(terms=("x",)), None)


def test_records_without_title_are_skipped_and_counted():
    page = copy.deepcopy(KEYWORD)
    page["results"][0]["title"] = None
    router = Router().reply("/works", json_body=page)
    result = source(router).fetch(KeywordSearch(terms=("x",)), None)
    assert len(result.records) == len(page["results"]) - 1
    assert any("跳过 1 条" in warning for warning in result.warnings)


def test_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        source(Router(), per_page=200)
    with pytest.raises(ConfigError):
        source(Router(), unknown_option=1)


# ---- 全文 ----

ATTENTION = fixture_json("openalex", "work_attention.json")


def fulltext(router: Router, **options) -> OpenAlexFullText:
    return OpenAlexFullText(
        options,
        contact_email="contact@example.org",
        transport=router.transport,
        resolver=public_resolver,
    )


def request(*aliases: Alias) -> FullTextRequest:
    return FullTextRequest("P", "Paper", aliases)


def test_fulltext_downloads_best_oa_pdf_by_doi():
    router = (
        Router()
        .reply("api.openalex.org/works/doi:", json_body=ATTENTION)
        .reply("arxiv.org/pdf/1706.03762", body=PDF)
    )
    found = fulltext(router).fetch(request(Alias("doi", "10.48550/arxiv.1706.03762")))
    assert found is not None
    assert (found.content, found.license_basis, found.provider) == (PDF, "open", "openalex")
    assert router.requests[0].url.path == "/works/doi:10.48550/arxiv.1706.03762"
    assert "best_oa_location" in router.params(0)["select"]


def test_fulltext_uses_arxiv_alias_when_there_is_no_doi():
    router = (
        Router()
        .reply("api.openalex.org/works/doi:", json_body=ATTENTION)
        .reply("arxiv.org/pdf/", body=PDF)
    )
    assert fulltext(router).fetch(request(Alias("arxiv", "1706.03762"))) is not None
    assert router.requests[0].url.path == "/works/doi:10.48550/arXiv.1706.03762"


def test_fulltext_skips_non_pdf_and_tries_next_open_location():
    router = (
        Router()
        .reply("api.openalex.org/works/", json_body=ATTENTION)
        .reply("arxiv.org/pdf/1706.03762", body=b"<html>not a pdf</html>")
        .reply("langtaosha.org.cn", body=PDF)
    )
    found = fulltext(router, max_links=3).fetch(request(Alias("doi", "10.48550/arxiv.1706.03762")))
    assert found is not None
    downloads = [str(r.url) for r in router.requests[1:]]
    assert downloads[0] == "https://arxiv.org/pdf/1706.03762"
    assert "langtaosha.org.cn" in downloads[-1]


def test_fulltext_respects_link_limit():
    router = Router().reply("api.openalex.org/works/", json_body=ATTENTION).reply("/", body=b"html")
    assert fulltext(router, max_links=1).fetch(request(Alias("doi", "10.1/x"))) is None
    assert len(router.requests) == 2


def test_fulltext_without_open_pdf_returns_none():
    closed = fixture_json("openalex", "work_closed.json")
    router = Router().reply("api.openalex.org/works/", json_body=closed)
    assert fulltext(router).fetch(request(Alias("doi", "10.1038/nature14539"))) is None
    assert len(router.requests) == 1


def test_fulltext_unknown_or_unidentified_paper_is_not_an_error():
    assert fulltext(Router()).fetch(request(Alias("doi", "10.9999/missing"))) is None
    router = Router()
    assert fulltext(router).fetch(request(Alias("s2", "abc"))) is None
    assert router.requests == []


def test_fulltext_can_look_up_by_pmid():
    router = Router().reply(
        "/works/pmid:123", json_body=fixture_json("openalex", "work_closed.json")
    )
    assert fulltext(router).fetch(request(Alias("pmid", "123"))) is None
    assert router.requests[0].url.path == "/works/pmid:123"


def test_fulltext_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        fulltext(Router(), max_links=0)
