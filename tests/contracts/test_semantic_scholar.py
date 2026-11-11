"""Semantic Scholar 信号源和开放获取全文。

录制数据来自 2026-11-05 无 key 的真实调用（共享限流池，间隔重试后录到），裁剪到几条：
- bulk_page1/2：同一次关键词检索的真实记录拆成两页，第一页的 token 取自另一次真实检索；
- citations、author_papers、author_search_ambiguous、paper_open_access*：原样录制，
  author_papers 的作者列表截到前 6 位。
唯一候选的作者检索响应在测试里按真实候选手写（真实检索里常见作者都有多个档案）。
"""

from datetime import date

import httpx
import pytest
from tests.helpers.http import Router, fixture_json, public_resolver

from litgleaner.adapters.fulltext.semantic_scholar import SemanticScholarFullText
from litgleaner.adapters.sources.semantic_scholar import SemanticScholarSource, bulk_query
from litgleaner.domain.errors import ConfigError, RateLimited, SchemaChanged
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import (
    AuthorWatch,
    CitationWatch,
    HistoricalCitations,
    HistoricalSearch,
    JournalWatch,
    KeywordSearch,
    ReferenceList,
)
from litgleaner.ports.fulltext import FullTextRequest

TODAY = date(2026, 11, 5)
PAGE1 = fixture_json("semantic_scholar", "bulk_page1.json")
PAGE2 = fixture_json("semantic_scholar", "bulk_page2.json")
CITATIONS = fixture_json("semantic_scholar", "citations.json")
AUTHOR_PAPERS = fixture_json("semantic_scholar", "author_papers.json")
AMBIGUOUS = fixture_json("semantic_scholar", "author_search_ambiguous.json")
UNIQUE = {"total": 1, "offset": 0, "data": [{"authorId": "2257301155", "name": "G. Hinton"}]}
PDF = b"%PDF-1.7\nbody"
# 关键词录制时的窗口是 2026-10-01 到 2026-11-05。
RECORDED_WINDOW = {"lookback_days": 35}


def source(router: Router, api_key: str | None = None, **options) -> SemanticScholarSource:
    return SemanticScholarSource(
        "semantic_scholar",
        options,
        contact_email="contact@example.org",
        api_key=api_key,
        transport=router.transport,
        today=lambda: TODAY,
    )


def paged(first: dict, second: dict, key: str) -> Router:
    """第一页没有 key 参数，之后的请求返回第二页。"""

    def handler(request: httpx.Request) -> httpx.Response:
        later = request.url.params.get(key) not in (None, "0")
        return httpx.Response(200, json=second if later else first)

    return Router().on("/graph/v1/", handler)


def test_declares_keyword_citation_author_and_reference_queries():
    assert set(source(Router()).capabilities) == {
        KeywordSearch,
        CitationWatch,
        AuthorWatch,
        ReferenceList,
        HistoricalSearch,
        HistoricalCitations,
    }
    with pytest.raises(ValueError, match="不支持"):
        source(Router()).fetch(JournalWatch(issns=("1234-5678",)), None)


def test_bulk_query_uses_documented_boolean_syntax():
    query = KeywordSearch(terms=("graph neural network", "message passing"), exclude=("x y",))
    assert bulk_query(query) == '("graph neural network" | "message passing") -("x y")'
    assert bulk_query(KeywordSearch(terms=("a",))) == '("a")'


def test_keyword_search_sends_window_and_follows_token():
    router = paged(PAGE1, PAGE2, "token")
    query = KeywordSearch(terms=("graph neural network",), exclude=("transfer learning",))
    result = source(router, **RECORDED_WINDOW).fetch(query, None)
    first, second = router.params(0), router.params(1)
    assert "/paper/search/bulk" in str(router.requests[0].url)
    assert first["query"] == '("graph neural network") -("transfer learning")'
    assert first["publicationDateOrYear"] == "2026-10-01:2026-11-05"
    assert "externalIds" in first["fields"] and "token" not in first
    assert second["token"] == PAGE1["token"]
    assert len(router.requests) == 2
    assert len(result.records) == 4
    assert result.next_cursor == "2026-11-05"
    assert result.raw and b"Differentiable Autoplanning" in result.raw


def test_record_fields_and_identifiers_are_mapped():
    router = paged(PAGE1, PAGE2, "token")
    result = source(router, **RECORDED_WINDOW).fetch(KeywordSearch(terms=("x",)), None)
    pmc = next(r for r in result.records if r.doi == "10.1016/j.ijpt.2026.101967")
    assert pmc.title.startswith("Differentiable Autoplanning Algorithm for Proton Therapy")
    assert pmc.published_at == date(2026, 10, 1)
    assert pmc.authors and pmc.abstract
    assert dict(pmc.external_ids) == {
        "s2": "0bea0bb0edb99b5d1f4d79409233f1041f41daa4",
        "pmid": "42959910",
        "pmcid": "PMC13718767",
    }
    preprint = next(r for r in result.records if r.arxiv_id == "2610.38993")
    assert preprint.doi is None and preprint.authors[0] == "Mason B. Peterson"
    journal = next(r for r in result.records if r.doi == "10.1016/j.ast.2026.113972")
    assert journal.venue == "Aerospace Science and Technology" and journal.abstract is None


def test_keyword_search_stops_at_limit_with_warning():
    router = paged(PAGE1, PAGE2, "token")
    result = source(router, max_records=1, **RECORDED_WINDOW).fetch(
        KeywordSearch(terms=("x",)), None
    )
    assert len(router.requests) == 1
    assert len(result.records) == 1
    assert any("上限" in warning for warning in result.warnings)


def test_citation_watch_pages_by_offset_and_filters_window_locally():
    citations = fixture_json("semantic_scholar", "citations.json")
    citations["data"][0]["citingPaper"]["publicationDate"] = "2026-09-01"
    last_page = {"offset": 5, "data": CITATIONS["data"][:1]}
    router = paged(citations, last_page, "offset")
    result = source(router).fetch(CitationWatch(dois=("10.48550/arXiv.1706.03762",)), None)
    first, second = router.params(0), router.params(1)
    assert router.requests[0].url.path.endswith("/paper/DOI:10.48550/arXiv.1706.03762/citations")
    assert first["publicationDateOrYear"] == "2026-10-29:2026-11-05"
    assert (first["offset"], first["limit"], second["offset"]) == ("0", "100", "5")
    assert len(router.requests) == 2
    dois = {record.doi for record in result.records}
    assert "10.1017/jfm.2026.12201" in dois
    assert "10.1007/s44295-026-00122-6" in dois  # 第二页里的原始日期在窗口内
    assert len(result.records) == 5


def test_citation_watch_skips_unknown_doi_with_warning():
    result = source(Router()).fetch(CitationWatch(dois=("10.9999/missing",)), None)
    assert result.records == ()
    assert any("10.9999/missing" in warning for warning in result.warnings)


def test_author_watch_uses_only_unambiguous_first_candidate():
    router = (
        Router()
        .reply("/author/search", json_body=UNIQUE)
        .reply("/author/2257301155/papers", json_body={**AUTHOR_PAPERS, "next": None})
    )
    result = source(router).fetch(AuthorWatch(names=("G. Hinton",)), None)
    assert router.params(0)["query"] == "G. Hinton"
    assert router.params(1)["publicationDateOrYear"] == "2026-10-29:2026-11-05"
    assert [r.title for r in result.records] == ["Hinton: “Is AI Like Us and Will It Like Us?”"]


def test_author_watch_skips_ambiguous_names():
    router = Router().reply("/author/search", json_body=AMBIGUOUS)
    result = source(router).fetch(AuthorWatch(names=("Geoffrey Hinton",)), None)
    assert result.records == ()
    assert len(router.requests) == 1
    assert any("Geoffrey Hinton" in warning for warning in result.warnings)


def test_hyphenated_author_names_are_searched_with_spaces():
    router = Router().reply("/author/search", json_body={"total": 0, "offset": 0, "data": []})
    result = source(router).fetch(AuthorWatch(names=("Jean-Pierre Martin",)), None)
    assert router.params(0)["query"] == "Jean Pierre Martin"
    assert any("找不到作者" in warning for warning in result.warnings)


def test_api_key_is_sent_in_header_and_scrubbed_from_raw_log():
    router = Router().reply("/paper/search/bulk", json_body={**PAGE2, "echo": "s2-secret"})
    result = source(router, api_key="s2-secret").fetch(KeywordSearch(terms=("x",)), None)
    assert router.requests[0].headers["x-api-key"] == "s2-secret"
    assert result.raw and b"s2-secret" not in result.raw
    anonymous = Router().reply("/paper/search/bulk", json_body=PAGE2)
    source(anonymous).fetch(KeywordSearch(terms=("x",)), None)
    assert "x-api-key" not in anonymous.requests[0].headers


def test_rate_limit_and_schema_errors():
    with pytest.raises(RateLimited):
        source(Router().reply("/paper/search/bulk", 429)).fetch(KeywordSearch(terms=("x",)), None)
    broken = Router().reply("/paper/search/bulk", json_body={"total": 1, "data": "oops"})
    with pytest.raises(SchemaChanged):
        source(broken).fetch(KeywordSearch(terms=("x",)), None)


def test_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        source(Router(), page_size=0)


# ---- 开放获取全文 ----


def fulltext(router: Router) -> SemanticScholarFullText:
    return SemanticScholarFullText(
        {}, contact_email=None, transport=router.transport, resolver=public_resolver
    )


def request(*aliases: Alias) -> FullTextRequest:
    return FullTextRequest("P", "Attention Is All You Need", aliases)


DOI = Alias("doi", "10.48550/arxiv.1706.03762")
ARXIV = Alias("arxiv", "1706.03762")


def test_fulltext_downloads_open_access_pdf_by_doi():
    router = (
        Router()
        .reply(
            "api.semanticscholar.org/graph/v1/paper/DOI:",
            json_body=fixture_json("semantic_scholar", "paper_open_access.json"),
        )
        .reply("arxiv.org/pdf/1706.03762", body=PDF)
    )
    found = fulltext(router).fetch(request(DOI, ARXIV))
    assert found is not None
    assert (found.content, found.license_basis, found.provider) == (PDF, "open", "semantic_scholar")
    assert router.requests[0].url.path.endswith("/paper/DOI:10.48550/arxiv.1706.03762")
    assert router.params(0) == {"fields": "openAccessPdf"}


def test_fulltext_falls_back_to_arxiv_when_doi_unknown():
    router = (
        Router()
        .reply(
            "/paper/ARXIV:1706.03762",
            json_body=fixture_json("semantic_scholar", "paper_open_access_arxiv.json"),
        )
        .reply("arxiv.org/pdf/", body=PDF)
    )
    found = fulltext(router).fetch(request(Alias("doi", "10.9999/unknown"), ARXIV))
    assert found is not None and found.content == PDF


def test_fulltext_returns_none_without_link_identifier_or_pdf():
    closed = {"paperId": "x", "openAccessPdf": {"url": "", "status": "CLOSED"}}
    assert fulltext(Router().reply("/paper/", json_body=closed)).fetch(request(DOI)) is None
    assert fulltext(Router()).fetch(request(Alias("pmid", "1"))) is None
    html = (
        Router()
        .reply("/paper/", json_body=fixture_json("semantic_scholar", "paper_open_access.json"))
        .reply("arxiv.org", body=b"<html>landing page</html>")
    )
    assert fulltext(html).fetch(request(DOI)) is None
    assert fulltext(Router()).fetch(request(DOI)) is None
