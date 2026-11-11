"""明确 URL 边界优先于正文标点裁剪，完整 URL 不得退化为较短别名。"""

import pytest

from litgleaner.application.extraction_facts import verify_extraction
from litgleaner.application.knowledge_ingest import ExtractionSource, SentBlock
from litgleaner.domain.document import Block, NormalizedDocument


def checked(url, quote):
    block = Block("text", quote, 0)
    document = NormalizedDocument(1, "a" * 64, "test", "Study", (block,), (), "ok")
    source = ExtractionSource(f"[page=0 results]\n{quote}", (SentBlock(block, quote),))
    item = {
        "kind": "code",
        "availability": "unknown",
        "text": "Model explanation",
        "url": url,
        "quote": quote,
        "page": 0,
    }
    return verify_extraction({"code_data": [item]}, document, source)["code_data"][0]


@pytest.mark.parametrize(
    "wrapper",
    [
        "<{url}>",
        '"{url}"',
        "'{url}'",
        "`{url}`",
        "[repository]({url})",
        "({url})",
        '[repository]({url} "a title")',
        "[repository]( {url} )",
    ],
)
@pytest.mark.parametrize("tail", ["!", ".", ",", ";", "?", "?key=abc!", "(v2)!"])
def test_explicit_url_boundaries_preserve_real_terminal_characters_and_reject_prefixes(
    wrapper, tail
):
    prefix = "https://code.example/Repo"
    actual = prefix + tail
    quote = "The source is available at " + wrapper.format(url=actual) + "."
    rejected = checked(prefix, quote)
    assert rejected["verified"] is True
    assert rejected["url"] is None and rejected["url_verified"] is False
    accepted = checked(actual, quote)
    assert accepted["verified"] is True
    assert accepted["url"] == actual and accepted["url_verified"] is True


@pytest.mark.parametrize(
    "actual",
    [
        "https://[::1]/Repo!",
        "https://code.example/Repo(a(b)c)!",
        "https://code.example/Repo[release]!",
        "https://code.example/Repo{release}!",
    ],
)
def test_markdown_boundary_preserves_balanced_uri_brackets_and_terminal_punctuation(actual):
    quote = f"The source is available at [repository]({actual})."
    result = checked(actual, quote)
    assert result["url"] == actual and result["url_verified"] is True


def test_url_candidate_cannot_use_a_trimmed_alias_when_actual_token_is_explicitly_delimited():
    actual = "https://code.example/Repo?key=abc!"
    result = checked(actual[:-1], f'Code is released at "{actual}" and nowhere else.')
    assert result["verified"] is True and result["url"] is None and not result["url_verified"]


@pytest.mark.parametrize("wrapper", ["{url}", "'{url}'", '"{url}"', "[repository]({url})"])
@pytest.mark.parametrize("tail", ["Smith's-Repo", "Repo?a=O'Brien"])
def test_internal_apostrophe_is_part_of_the_full_uri_and_cannot_create_a_prefix_alias(
    wrapper, tail
):
    actual = f"https://code.example/{tail}"
    prefix = actual.split("'", maxsplit=1)[0]
    quote = "The source is available at " + wrapper.format(url=actual)
    rejected = checked(prefix, quote)
    assert rejected["verified"] and rejected["url"] is None and not rejected["url_verified"]
    accepted = checked(actual, quote)
    assert accepted["url"] == actual and accepted["url_verified"]
