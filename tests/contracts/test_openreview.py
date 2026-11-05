"""OpenReview 信号源。search.json 裁剪自 2026-11-05 对 api2.openreview.net/notes/search 的真实调用
（term=diffusion，source=forum），只删减了 content 字段、摘要长度和作者数量。
最后一条来自同日按标题检索的结果，作者字段是带 fullname 的对象（部分研讨会的格式）。"""

import copy
from datetime import date

import pytest
from tests.helpers.http import Router, fixture_json

from litfactory.adapters.sources.openreview import OpenReviewSource
from litfactory.domain.errors import ConfigError, RateLimited, SchemaChanged
from litfactory.domain.query import AuthorWatch, KeywordSearch

SEARCH = fixture_json("openreview", "search.json")
# 两篇 ICLR 2027 投稿在 2026-10-03 公开，窗口 2026-09-29 至 2026-10-06 只覆盖它们。
TODAY = date(2026, 10, 6)


def source(router: Router, **options) -> OpenReviewSource:
    return OpenReviewSource(
        "openreview",
        options,
        contact_email="contact@example.org",
        transport=router.transport,
        today=lambda: TODAY,
    )


def titles(result) -> list[str]:
    return [record.title for record in result.records]


def test_declares_keyword_search_only():
    assert {KeywordSearch} == set(source(Router()).capabilities)
    with pytest.raises(ValueError, match="不支持"):
        source(Router()).fetch(AuthorWatch(names=("A B",)), None)


def test_each_term_is_searched_among_forum_notes():
    router = Router().reply("/notes/search", json_body=SEARCH)
    source(router, page_size=50).fetch(KeywordSearch(terms=("diffusion", "caching")), None)
    first, second = router.params(0), router.params(1)
    assert (first["term"], second["term"]) == ("diffusion", "caching")
    assert first["source"] == "forum"
    assert (first["limit"], first["offset"]) == ("50", "0")


def test_window_uses_public_date_and_skips_imported_records():
    router = Router().reply("/notes/search", json_body=SEARCH)
    result = source(router).fetch(KeywordSearch(terms=("diffusion",)), None)
    assert titles(result) == [
        "RIDE: Reference-Anchored Inference-Time Diffusion Editing for Scaffold Hopping",
        "The Golden Path Hypothesis: Reusable Schedules in Diffusion Caching",
    ]
    assert len(router.requests) == 1  # 不满一页就不再翻
    assert result.next_cursor == "2026-10-06"
    assert result.raw and b"Golden Path" in result.raw


def test_record_fields_are_mapped():
    router = Router().reply("/notes/search", json_body=SEARCH)
    result = source(router).fetch(KeywordSearch(terms=("diffusion",)), None)
    record = result.records[1]
    assert record.external_ids == {"openreview": "c9hwAzUiBt"}
    assert record.venue == "ICLR 2027 Conference Submission"
    assert record.published_at == date(2026, 10, 3)
    assert record.abstract and "\n" not in record.abstract
    assert record.authors == ()  # 双盲投稿没有公开作者


def test_published_paper_keeps_authors_and_publication_date():
    body = {"notes": [copy.deepcopy(SEARCH["notes"][4])], "count": 1}
    router = Router().reply("/notes/search", json_body=body)
    old = OpenReviewSource(
        "openreview",
        {},
        contact_email=None,
        transport=router.transport,
        today=lambda: date(2024, 11, 8),
    )
    record = old.fetch(KeywordSearch(terms=("diffusion",)), None).records[0]
    assert record.authors[:2] == ("HANWEN LIANG", "Yuyang Yin")
    assert record.venue == "NeurIPS 2024 poster"
    assert record.published_at == date(2024, 9, 25)


def test_structured_authors_use_full_name():
    router = Router().reply("/notes/search", json_body=SEARCH)
    recent = OpenReviewSource(
        "openreview",
        {},
        contact_email=None,
        transport=router.transport,
        today=lambda: date(2026, 11, 5),
    )
    records = recent.fetch(KeywordSearch(terms=("diffusion",)), None).records
    workshop = next(r for r in records if r.external_ids["openreview"] == "dgNMZ4i9bL")
    assert workshop.authors == ("Arseny Ivanov", "Alexander Kolesov")
    assert workshop.venue == "DiffuLM Poster"


def test_exclusions_and_non_public_notes_are_filtered_locally():
    body = copy.deepcopy(SEARCH)
    body["notes"][0]["readers"] = ["ICLR.cc/2027/Conference"]
    router = Router().reply("/notes/search", json_body=body)
    query = KeywordSearch(terms=("diffusion",), exclude=("golden path",))
    assert source(router).fetch(query, None).records == ()


def test_pages_through_relevance_results_up_to_page_cap():
    full = {"notes": SEARCH["notes"][:2], "count": 3608}
    router = Router().reply("/notes/search", json_body=full)
    result = source(router, page_size=2, max_pages_per_term=3).fetch(
        KeywordSearch(terms=("diffusion",)), None
    )
    offsets = [router.params(i)["offset"] for i in range(len(router.requests))]
    assert offsets == ["0", "2", "4"]
    assert any("翻页上限" in warning for warning in result.warnings)


def test_short_page_ends_paging_and_limit_truncates():
    router = Router().reply("/notes/search", json_body=SEARCH)
    result = source(router, page_size=10, max_records=1).fetch(
        KeywordSearch(terms=("diffusion", "caching")), None
    )
    assert len(router.requests) == 1
    assert len(result.records) == 1
    assert any("单次上限" in warning for warning in result.warnings)


def test_rate_limit_and_schema_errors():
    with pytest.raises(RateLimited):
        source(Router().reply("/notes/search", 429)).fetch(KeywordSearch(terms=("x",)), None)
    broken = Router().reply("/notes/search", json_body={"count": 1})
    with pytest.raises(SchemaChanged):
        source(broken).fetch(KeywordSearch(terms=("x",)), None)


def test_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        source(Router(), page_size=5000)
