"""Unpaywall 全文。录制数据来自 2026-11-05 的真实调用，邮箱已替换、作者列表已裁剪。
bad_email.json 是使用占位邮箱时的真实 422 响应。"""

import copy

import pytest
from tests.helpers.http import Router, fixture_json, public_resolver

from litgleaner.adapters.fulltext.unpaywall import UnpaywallFullText
from litgleaner.domain.errors import ConfigError, SchemaChanged
from litgleaner.domain.identity import Alias
from litgleaner.ports.fulltext import FullTextRequest

PDF = b"%PDF-1.5\nbody"
ARXIV_COPY = fixture_json("unpaywall", "arxiv_copy.json")


def unpaywall(router: Router, email: str | None = "contact@example.org", **options):
    return UnpaywallFullText(
        options, contact_email=email, transport=router.transport, resolver=public_resolver
    )


def request(doi: str | None = "10.1016/j.patcog.2023.109437") -> FullTextRequest:
    aliases = (Alias("doi", doi),) if doi else (Alias("arxiv", "2106.00537"),)
    return FullTextRequest("P", "Paper", aliases)


def test_downloads_best_oa_pdf_with_contact_email():
    router = (
        Router()
        .reply("api.unpaywall.org/v2/", json_body=ARXIV_COPY)
        .reply("arxiv.org/pdf/2106.00537", body=PDF)
    )
    found = unpaywall(router).fetch(request())
    assert found is not None
    assert (found.content, found.license_basis, found.provider) == (PDF, "open", "unpaywall")
    lookup = router.requests[0]
    assert lookup.url.path == "/v2/10.1016/j.patcog.2023.109437"
    assert router.params(0)["email"] == "contact@example.org"


def test_skips_non_pdf_and_tries_other_oa_locations():
    record = copy.deepcopy(ARXIV_COPY)
    record["oa_locations"].append({"url_for_pdf": "https://repo.example.edu/paper.pdf"})
    router = (
        Router()
        .reply("api.unpaywall.org/v2/", json_body=record)
        .reply("arxiv.org/pdf/", body=b"<html>landing page</html>")
        .reply("repo.example.edu", body=PDF)
    )
    found = unpaywall(router).fetch(request())
    assert found is not None
    assert str(router.requests[-1].url) == "https://repo.example.edu/paper.pdf"


def test_respects_link_limit():
    record = copy.deepcopy(ARXIV_COPY)
    record["oa_locations"].append({"url_for_pdf": "https://repo.example.edu/paper.pdf"})
    router = Router().reply("api.unpaywall.org/v2/", json_body=record).reply("/", body=b"html")
    assert unpaywall(router, max_links=1).fetch(request()) is None
    assert len(router.requests) == 2


@pytest.mark.parametrize("name", ["no_oa.json", "landing_only.json"])
def test_records_without_pdf_links_return_none(name: str):
    router = Router().reply("api.unpaywall.org/v2/", json_body=fixture_json("unpaywall", name))
    assert unpaywall(router).fetch(request()) is None
    assert len(router.requests) == 1


def test_missing_doi_and_unknown_doi_return_none():
    router = Router()
    assert unpaywall(router).fetch(request(None)) is None
    assert router.requests == []
    assert unpaywall(Router()).fetch(request("10.9999/missing")) is None


def test_unexpected_body_is_schema_change():
    router = Router().reply("api.unpaywall.org/v2/", json_body=["not", "an", "object"])
    with pytest.raises(SchemaChanged):
        unpaywall(router).fetch(request())


def test_rejected_email_is_reported_as_schema_change():
    router = Router().reply(
        "api.unpaywall.org/v2/", 422, json_body=fixture_json("unpaywall", "bad_email.json")
    )
    with pytest.raises(SchemaChanged, match="email"):
        unpaywall(router).fetch(request())


def test_contact_email_is_required():
    with pytest.raises(ConfigError, match="邮箱"):
        unpaywall(Router(), email=None)
    with pytest.raises(ConfigError, match="邮箱"):
        unpaywall(Router(), email="  ")


def test_invalid_options_are_config_errors():
    with pytest.raises(ConfigError):
        unpaywall(Router(), max_links=0)
