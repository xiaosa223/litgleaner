"""CORE 信号源和开放获取全文。

录制数据来自 2026-11-05 不带 key 的真实调用（未登录用户每分钟 10 次、每天 100 次）：
- search_page1/2：q=(title:"graph neural network" OR abstract:"graph neural network")
  AND yearPublished>=2026 AND yearPublished<=2026，sort=publishedDate:desc，每页 10 条；
- work_by_doi：q=doi:"10.5463/thesis.1850"；work_not_found：一次 0 命中的 DOI 检索。
裁剪：去掉了很大的 fullText、references 字段，摘要截到 600 字。
带 key 的请求（Authorization: Bearer）没有真实验证过，key 的写法按文档“How to call the API”。
"""

from datetime import date

import httpx
import pytest
from tests.helpers.http import Router, fixture_json, public_resolver

from litgleaner.adapters.fulltext.core import CoreFullText
from litgleaner.adapters.sources.core import CoreSource
from litgleaner.domain.errors import AuthFailed, ConfigError, RateLimited, SchemaChanged
from litgleaner.domain.identity import Alias
from litgleaner.domain.query import CitationWatch, KeywordSearch
from litgleaner.ports.fulltext import FullTextRequest

TODAY = date(2026, 11, 5)
PAGE1 = fixture_json("core", "search_page1.json")
PAGE2 = fixture_json("core", "search_page2.json")
KEY = "core-secret-key"
PDF = b"%PDF-1.7\nbody"
QUERY = KeywordSearch(terms=("graph neural network",))


def source(router: Router, api_key: str = KEY, **options) -> CoreSource:
    return CoreSource(
        "core",
        {"page_size": 10, **options},
        contact_email="contact@example.org",
        api_key=api_key,
        transport=router.transport,
        today=lambda: TODAY,
    )


def paged(*pages: dict) -> Router:
    def handler(request: httpx.Request) -> httpx.Response:
        index = int(request.url.params["offset"]) // 10
        empty = {**PAGE2, "offset": index * 10, "results": []}
        return httpx.Response(200, json=pages[index] if index < len(pages) else empty)

    return Router().on("api.core.ac.uk/v3/search/works/", handler)


def older_page2() -> dict:
    """第二页最后两条改成窗口之前的日期，模拟翻到窗口外。"""
    page = fixture_json("core", "search_page2.json")
    for work in page["results"][-2:]:
        work["publishedDate"] = "2026-10-20T00:00:00+00:00"
    return page


def test_declares_keyword_search_only():
    assert set(source(Router()).capabilities) == {KeywordSearch}
    with pytest.raises(ValueError, match="不支持"):
        source(Router()).fetch(CitationWatch(dois=("10.1/x",)), None)


def test_query_uses_title_abstract_fields_year_range_and_date_sort():
    router = paged(PAGE1, older_page2())
    source(router).fetch(KeywordSearch(terms=("graph neural network", "GNN")), None)
    params = router.params(0)
    assert params["q"] == (
        '((title:"graph neural network" OR abstract:"graph neural network") OR '
        '(title:"GNN" OR abstract:"GNN")) AND yearPublished>=2026 AND yearPublished<=2026'
    )
    assert (params["sort"], params["limit"], params["offset"]) == ("publishedDate:desc", "10", "0")
    assert router.requests[0].headers["Authorization"] == f"Bearer {KEY}"


def test_pages_until_records_fall_before_window_and_filters_locally():
    router = paged(PAGE1, older_page2(), PAGE2)
    result = source(router).fetch(QUERY, None)
    assert [router.params(i)["offset"] for i in range(len(router.requests))] == ["0", "10"]
    published = {record.published_at for record in result.records}
    assert published == {date(2026, 11, 1)}  # 未来日期和窗口之前的都被过滤
    assert len(result.records) == 1 + 8
    assert result.next_cursor == "2026-11-05"
    assert result.raw and KEY.encode() not in result.raw


def test_record_fields_are_mapped():
    result = source(paged(PAGE1, older_page2())).fetch(QUERY, None)
    first = next(r for r in result.records if r.doi == "10.32738/jeppm-2025-309")
    assert first.title.startswith("DLAGNN: A Dual-Layer Attention Graph Neural Network")
    assert first.authors == ("Junyi Yang",)
    assert first.published_at == date(2026, 11, 1)
    assert first.abstract and first.abstract.startswith("The customer-centric")
    assert dict(first.external_ids) == {"core": "344493920"}


def test_identifiers_and_venue_when_present():
    page = fixture_json("core", "search_page2.json")
    work = page["results"][0]
    work.update(
        arxivId="2611.00001",
        pubmedId="12345678",
        journals=[{"title": "Ecological Modelling", "identifiers": ["issn:0304-3800"]}],
    )
    result = source(paged(page)).fetch(QUERY, "2026-11-04")
    record = next(r for r in result.records if r.arxiv_id == "2611.00001")
    assert dict(record.external_ids) == {"core": "385795570", "pmid": "12345678"}
    assert (record.venue, record.issn) == ("Ecological Modelling", "0304-3800")


def test_exclusions_are_applied_locally():
    query = KeywordSearch(terms=("graph neural network",), exclude=("underwater wireless",))
    router = paged(PAGE1, older_page2())
    result = source(router).fetch(query, None)
    assert "underwater" not in router.params(0)["q"]
    assert not any("underwater" in r.title.casefold() for r in result.records)
    assert len(result.records) == 8


def test_stops_at_limit_with_warning():
    router = paged(PAGE1, PAGE2, PAGE2)
    result = source(router, max_records=10).fetch(QUERY, None)
    assert len(router.requests) == 1
    assert any("上限" in warning for warning in result.warnings)


def test_missing_key_is_auth_failure_without_request():
    router = Router()
    with pytest.raises(AuthFailed, match="CORE_API_KEY"):
        source(router, api_key="").fetch(QUERY, None)
    assert router.requests == []


def test_http_errors_are_classified():
    invalid = Router().reply("/search/works/", 401, json_body={"message": "not valid"})
    with pytest.raises(AuthFailed):
        source(invalid).fetch(QUERY, None)
    with pytest.raises(RateLimited):
        source(Router().reply("/search/works/", 429)).fetch(QUERY, None)
    broken = Router().reply("/search/works/", json_body={"totalHits": 1, "results": None})
    with pytest.raises(SchemaChanged):
        source(broken).fetch(QUERY, None)


def test_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        source(Router(), page_size=500)


# ---- 开放获取全文 ----


def fulltext(router: Router, api_key: str = KEY) -> CoreFullText:
    return CoreFullText(
        {},
        contact_email=None,
        api_key=api_key,
        transport=router.transport,
        resolver=public_resolver,
    )


def request(*aliases: Alias) -> FullTextRequest:
    return FullTextRequest("P", "Paper", aliases)


DOI = Alias("doi", "10.5463/THESIS.1850")


def test_fulltext_finds_work_by_doi_and_downloads_pdf():
    router = (
        Router()
        .reply(
            "api.core.ac.uk/v3/search/works/", json_body=fixture_json("core", "work_by_doi.json")
        )
        .reply("core.ac.uk/download/748655335.pdf", body=PDF)
    )
    found = fulltext(router).fetch(request(DOI))
    assert found is not None
    assert (found.content, found.license_basis, found.provider) == (PDF, "open", "core")
    assert router.params(0)["q"] == 'doi:"10.5463/thesis.1850"'
    downloads = [r for r in router.requests if "download" in r.url.path]
    assert len(downloads) == 1 and "Authorization" not in downloads[0].headers


def test_fulltext_ignores_works_with_other_doi_and_non_pdf():
    work = fixture_json("core", "work_by_doi.json")
    work["results"][0]["doi"] = "10.5463/thesis.9999"
    router = Router().reply("/search/works/", json_body=work).reply("core.ac.uk/download", body=PDF)
    assert fulltext(router).fetch(request(DOI)) is None
    html = (
        Router()
        .reply("/search/works/", json_body=fixture_json("core", "work_by_doi.json"))
        .reply("core.ac.uk/download", body=b"<html>reader</html>")
    )
    assert fulltext(html).fetch(request(DOI)) is None


def test_fulltext_without_match_or_doi_returns_none():
    empty = Router().reply("/search/works/", json_body=fixture_json("core", "work_not_found.json"))
    assert fulltext(empty).fetch(request(DOI)) is None
    no_doi = Router()
    assert fulltext(no_doi).fetch(request(Alias("arxiv", "2101.00001"))) is None
    assert no_doi.requests == []


def test_fulltext_without_key_is_auth_failure():
    with pytest.raises(AuthFailed):
        fulltext(Router(), api_key="").fetch(request(DOI))
