"""arXiv 信号源和全文。

keyword.xml 录自 2026-11-05 的真实调用（all:"large language model" 加 submittedDate 范围，
max_results=4），只把作者删减到每篇最多 3 位。arxiv:doi 和 arxiv:journal_ref 在测试里注入。
error.xml 按 API 用户手册的错误示例构造，未经真实调用验证。
"""

from datetime import date

import pytest
from tests.helpers.http import Router, fixture_bytes, public_resolver

from litgleaner.adapters.fulltext.arxiv import ArxivFullText
from litgleaner.adapters.sources.arxiv import ArxivSource
from litgleaner.domain.errors import ConfigError, RateLimited, SchemaChanged, TransientError
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import AuthorWatch, IdentifierLookup, JournalWatch, KeywordSearch
from litgleaner.ports.fulltext import FullTextRequest

PDF = b"%PDF-1.5\nbody"


# ---- 全文 ----


def fulltext(router: Router, **options) -> ArxivFullText:
    return ArxivFullText(
        options,
        contact_email="contact@example.org",
        transport=router.transport,
        resolver=public_resolver,
        sleep=lambda _: None,
    )


def request(*aliases: Alias) -> FullTextRequest:
    return FullTextRequest("P", "Paper", aliases)


def test_fulltext_downloads_pdf_by_arxiv_id():
    router = Router().reply("arxiv.org/pdf/1706.03762", body=PDF)
    found = fulltext(router).fetch(request(Alias("arxiv", "1706.03762")))
    assert found is not None
    assert (found.content, found.license_basis, found.provider) == (PDF, "open", "arxiv")
    assert str(router.requests[0].url) == "https://arxiv.org/pdf/1706.03762"


def test_fulltext_extracts_id_from_arxiv_datacite_doi():
    router = Router().reply("arxiv.org/pdf/", body=PDF)
    found = fulltext(router).fetch(request(Alias("doi", "10.48550/arxiv.2411.01234")))
    assert found is not None
    assert str(router.requests[0].url) == "https://arxiv.org/pdf/2411.01234"


def test_fulltext_handles_old_style_ids():
    router = Router().reply("arxiv.org/pdf/", body=PDF)
    assert fulltext(router).fetch(request(Alias("arxiv", "hep-th/9901001"))) is not None
    assert str(router.requests[0].url) == "https://arxiv.org/pdf/hep-th/9901001"


def test_fulltext_without_arxiv_identifier_returns_none():
    router = Router()
    assert fulltext(router).fetch(request(Alias("doi", "10.1038/nature14539"))) is None
    assert fulltext(router).fetch(request()) is None
    assert router.requests == []


def test_fulltext_non_pdf_or_missing_returns_none():
    html = Router().reply("arxiv.org/pdf/", body=b"<html>not a pdf</html>")
    assert fulltext(html).fetch(request(Alias("arxiv", "2411.01234"))) is None
    assert fulltext(Router()).fetch(request(Alias("arxiv", "2411.99999"))) is None


def test_fulltext_spaces_out_downloads():
    waits: list[float] = []
    clock = iter([100.0, 101.0, 101.0])
    provider = ArxivFullText(
        {},
        contact_email=None,
        transport=Router().reply("arxiv.org/pdf/", body=PDF).transport,
        resolver=public_resolver,
        sleep=waits.append,
        monotonic=lambda: next(clock),
    )
    provider.fetch(request(Alias("arxiv", "2411.00001")))
    provider.fetch(request(Alias("arxiv", "2411.00002")))
    assert waits == [pytest.approx(2.0)]


def test_fulltext_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        fulltext(Router(), base_url=123)


def test_fulltext_rate_limit_is_raised():
    router = Router().reply("arxiv.org/pdf/", 429, headers={"Retry-After": "60"})
    with pytest.raises(RateLimited):
        fulltext(router).fetch(request(Alias("arxiv", "2411.01234")))


# ---- 信号源 ----

TODAY = date(2026, 11, 5)
ERROR = fixture_bytes("arxiv", "error.xml")
KEYWORD = fixture_bytes("arxiv", "keyword.xml")
# 同一份数据，但 totalResults 改成 4：一页就取完。
COMPLETE = KEYWORD.replace(b"<opensearch:totalResults>261<", b"<opensearch:totalResults>4<")
LLM = KeywordSearch(terms=("large language model",))


def source(router: Router, **options) -> ArxivSource:
    return ArxivSource(
        "arxiv",
        options,
        contact_email="contact@example.org",
        transport=router.transport,
        today=lambda: TODAY,
    )


def test_declares_keyword_author_and_identifier_queries_only():
    assert {KeywordSearch, AuthorWatch, IdentifierLookup} == set(source(Router()).capabilities)
    with pytest.raises(ValueError, match="不支持"):
        source(Router()).fetch(JournalWatch(issns=("1234-5678",)), None)


def test_keyword_search_uses_boolean_syntax_date_range_and_sorting():
    router = Router().reply("/query", body=COMPLETE)
    query = KeywordSearch(terms=("large language model", "LLM agent"), exclude=("survey",))
    result = source(router).fetch(query, None)
    params = router.params(0)
    assert params["search_query"] == (
        '((all:"large language model" OR all:"LLM agent") ANDNOT (all:"survey")) '
        "AND submittedDate:[202610290000 TO 202611052359]"
    )
    assert (params["sortBy"], params["sortOrder"]) == ("submittedDate", "descending")
    assert (params["start"], params["max_results"]) == ("0", "100")
    assert len(router.requests) == 1  # 已取完 totalResults，不再翻页
    assert result.next_cursor == "2026-11-05"
    assert result.raw and b"Selective Robustness" in result.raw


def test_author_watch_uses_author_field():
    router = Router().reply("/query", body=COMPLETE)
    source(router).fetch(AuthorWatch(names=("Yann LeCun", 'Geoffrey "G" Hinton')), "2026-11-03")
    assert router.params(0)["search_query"] == (
        '(au:"Yann LeCun" OR au:"Geoffrey G Hinton") '
        "AND submittedDate:[202610310000 TO 202611052359]"
    )


def test_record_fields_are_mapped():
    result = source(Router().reply("/query", body=COMPLETE)).fetch(LLM, None)
    first = result.records[0]
    assert first.title == (
        "A Framework for Context Enrichment and Selective Robustness in LLM Social Agents"
    )
    assert first.arxiv_id == "2611.06914"
    assert first.authors == ("Mattia Greiche", "Reihaneh Rabbany", "Maximilian Puelma Touzel")
    assert first.published_at == date(2026, 11, 4)
    assert first.venue == "arXiv cs.MA"
    assert first.doi is None
    assert first.abstract and first.abstract.startswith("Researchers use large language")
    assert "\n" not in first.abstract


def test_doi_and_journal_ref_are_used_when_present():
    extra = (
        b'<arxiv:primary_category term="cs.MA"/>'
        b"<arxiv:doi>10.1000/demo.1</arxiv:doi>"
        b"<arxiv:journal_ref>Journal of Demos 1 (2026) 1-12</arxiv:journal_ref>"
    )
    body = COMPLETE.replace(b'<arxiv:primary_category term="cs.MA"/>', extra, 1)
    first = source(Router().reply("/query", body=body)).fetch(LLM, None).records[0]
    assert first.doi == "10.1000/demo.1"
    assert first.venue == "Journal of Demos 1 (2026) 1-12"


def test_paging_stops_at_entries_older_than_window():
    body = KEYWORD.replace(b"2026-11-04T18:41:40Z", b"2026-10-01T18:41:40Z")
    router = Router().reply("/query", body=body)
    result = source(router, page_size=4).fetch(LLM, None)
    assert len(result.records) == 2
    assert len(router.requests) == 1


def test_paging_continues_and_truncates_at_limit():
    router = Router().reply("/query", body=KEYWORD)
    result = source(router, page_size=4, max_records=6).fetch(LLM, None)
    pages = [(router.params(i)["start"], router.params(i)["max_results"]) for i in (0, 1)]
    assert pages == [("0", "4"), ("4", "2")]
    assert len(router.requests) == 2
    assert any("上限" in warning for warning in result.warnings)


def test_empty_page_before_total_is_transient():
    body = KEYWORD.split(b"<entry>")[0] + b"</feed>"
    with pytest.raises(TransientError, match="空页"):
        source(Router().reply("/query", body=body)).fetch(LLM, None)


def test_error_entry_with_http_200_is_schema_changed():
    with pytest.raises(SchemaChanged, match="incorrect id format"):
        source(Router().reply("/query", body=ERROR)).fetch(KeywordSearch(terms=("x",)), None)


def test_rate_limit_and_broken_responses():
    with pytest.raises(RateLimited):
        source(Router().reply("/query", 429)).fetch(KeywordSearch(terms=("x",)), None)
    with pytest.raises(SchemaChanged):
        source(Router().reply("/query", body=b"<html>oops</html>")).fetch(
            AuthorWatch(names=("A B",)), None
        )
    with pytest.raises(TransientError):
        source(Router().reply("/query", 503)).fetch(KeywordSearch(terms=("x",)), None)


def test_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        source(Router(), min_interval_seconds=1)
    with pytest.raises(ConfigError):
        source(Router(), page_size=0)


# ---- 按标识符查（添加论文，第 12.8 节）：id_list 与检索返回同样的 Atom 格式 ----


def test_identifier_lookup_uses_id_list_without_date_window():
    feed = fixture_bytes("arxiv", "keyword.xml")
    router = Router().reply("/query", body=feed)
    result = ArxivSource("arxiv", {}, contact_email=None, transport=router.transport).fetch(
        IdentifierLookup(arxiv_id="1706.03762"), None
    )
    params = router.params(0)
    assert params["id_list"] == "1706.03762"
    assert "search_query" not in params
    assert len(result.records) == 1  # 只取第一条
    assert result.next_cursor is None


def test_identifier_lookup_without_arxiv_id_returns_nothing():
    source = ArxivSource("arxiv", {}, contact_email=None, transport=Router().transport)
    assert source.fetch(IdentifierLookup(doi="10.1/x"), None).records == ()
