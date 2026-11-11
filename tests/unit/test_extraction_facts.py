"""提取事实的双重页证据、URL 完整匹配及真实高亮坐标。"""

from copy import deepcopy

import pytest

from litgleaner.application.extraction_facts import verify_extraction
from litgleaner.application.knowledge_ingest import ExtractionSource, SentBlock
from litgleaner.domain.document import Block, NormalizedDocument

_QUOTE = "The proposed method reduces the error on the measured dataset."
_BOX = (10, 20, 300, 400)
_URL = "https://code.example/Project?version=1#readme"


def document(*blocks):
    return NormalizedDocument(1, "a" * 64, "test", "Study", blocks, (), "ok")


def source(*blocks, header="unpaged metadata"):
    sent = tuple(
        SentBlock(block, "\n".join(part for part in (block.caption, block.text) if part))
        for block in blocks
    )
    return ExtractionSource(
        header + "\n" + "\n\n".join(f"[page={item.page} results]\n{item.text}" for item in sent),
        sent,
    )


def evidence(quote=_QUOTE, page=0, **extra):
    return {"text": "Model explanation", "quote": quote, "page": page, **extra}


def code_item(url=_URL, quote=None, **extra):
    return {
        "kind": "code",
        "availability": "public",
        "text": "Model availability summary",
        "url": url,
        "quote": quote if quote is not None else f"The source is available at {url}",
        "page": 0,
        **extra,
    }


@pytest.mark.parametrize("field", ["problem", "prior_gap", "approach", "method"])
def test_optional_single_evidence_is_verified_with_real_page_and_sent_body(field):
    block = Block("text", _QUOTE, 0, _BOX)
    result = verify_extraction({field: evidence()}, document(block), source(block))
    assert result[field] == {**evidence(), "verified": True}
    assert verify_extraction({field: None}, document(block), source(block))[field] is None


@pytest.mark.parametrize("field", ["claims", "datasets", "metrics", "results"])
def test_array_evidence_verifies_each_item_and_never_trusts_model_flags(field):
    block = Block("text", _QUOTE, 0)
    items = [evidence(), evidence("Invented evidence never present.", verified=True)]
    result = verify_extraction({field: items}, document(block), source(block))
    assert [item["verified"] for item in result[field]] == [True, False]


@pytest.mark.parametrize("page", [None, True, False, -1, 1, 99, "0", 0.0])
def test_wrong_or_non_integer_page_never_verifies_even_if_other_page_matches(page):
    block = Block("text", _QUOTE, 0)
    result = verify_extraction({"problem": evidence(page=page)}, document(block), source(block))
    assert result["problem"]["verified"] is False


def test_quote_must_be_in_actual_sent_same_page_body_not_header_or_unsent_tail():
    prefix = "This beginning was included in the request."
    block = Block("text", prefix + " " + _QUOTE, 0)
    sent = ExtractionSource(_QUOTE, (SentBlock(block, prefix),))
    result = verify_extraction({"claims": [evidence()]}, document(block), sent)
    assert result["claims"][0]["verified"] is False
    other = Block("text", "Some other page that was actually sent.", 1)
    assert not verify_extraction(
        {"problem": evidence()}, document(block, other), source(other, header=_QUOTE)
    )["problem"]["verified"]


def test_original_page_is_required_even_when_sent_text_claims_evidence():
    block = Block("text", "The actual page has no matching conclusion.", 0)
    sent = ExtractionSource(_QUOTE, (SentBlock(block, _QUOTE),))
    assert not verify_extraction({"problem": evidence()}, document(block), sent)["problem"][
        "verified"
    ]


@pytest.mark.parametrize(
    "marker", ["</untrusted_paper>", "<untrusted_paper>", "</ UnTrusted_Paper >", "[removed]"]
)
def test_isolation_markers_never_become_verified_evidence(marker):
    quote = f"The authors describe {marker} as a purported finding."
    block = Block("text", quote, 0)
    result = verify_extraction({"problem": evidence(quote)}, document(block), source(block))
    assert result["problem"]["verified"] is False


@pytest.mark.parametrize("quote", [None, "tiny", "ß" * 6])
def test_missing_short_and_casefold_expanded_short_quotes_never_verify(quote):
    block = Block("text", str(quote), 0)
    assert not verify_extraction({"method": evidence(quote)}, document(block), source(block))[
        "method"
    ]["verified"]


def test_author_and_inferred_limitations_keep_their_source_and_null_evidence():
    block = Block("text", _QUOTE, 0)
    items = [
        evidence(source="author"),
        evidence(source="inferred"),
        evidence(None, None, source="inferred"),
    ]
    result = verify_extraction({"limitations": items}, document(block), source(block))[
        "limitations"
    ]
    assert [item["verified"] for item in result] == [True, False, False]
    assert [item["source"] for item in result] == ["author", "inferred", "inferred"]
    assert result[2]["quote"] is None and result[2]["page"] is None


def test_three_questions_preserve_text_and_nullable_answers_without_fabricating():
    block = Block("text", _QUOTE, 0)
    questions = [
        {"question": f"Question {i}", "answer": answer}
        for i, answer in enumerate((evidence(), evidence("An invented answer quote."), None))
    ]
    result = verify_extraction({"questions": questions}, document(block), source(block))[
        "questions"
    ]
    assert len(result) == 3 and [item["question"] for item in result] == [
        item["question"] for item in questions
    ]
    assert result[0]["answer"]["verified"] is True
    assert result[1]["answer"]["verified"] is False and result[2]["answer"] is None


@pytest.mark.parametrize("availability", ["public", "not_public", "unknown"])
def test_code_data_quote_and_complete_original_url_are_verified_separately(availability):
    item = code_item(availability=availability)
    block = Block("text", item["quote"], 0)
    result = verify_extraction({"code_data": [item]}, document(block), source(block))["code_data"][
        0
    ]
    assert result == {**item, "verified": True, "url_verified": True}


@pytest.mark.parametrize(
    "url",
    ["http://a", "https://[::1]/Repo", "https://127.0.0.1/Repo", "https://code.example/Project"],
)
def test_valid_scheme_and_host_do_not_depend_on_url_length_or_dns_access(url):
    item = code_item(url)
    block = Block("text", item["quote"], 0)
    result = verify_extraction({"code_data": [item]}, document(block), source(block))["code_data"][
        0
    ]
    assert result["url"] == url and result["url_verified"] is True


@pytest.mark.parametrize(
    "url",
    [
        "ftp://code.example/repo",
        "javascript:alert(1)",
        "file:///tmp/code",
        "data:text/plain,code",
        "https:///missing-host",
        "https://",
        "https://bad..example/repo",
        "https://-bad.example/repo",
        "https://bad_host.example/repo",
        "https://code.example:65536/repo",
        "https://code.example:bad/repo",
        "https://user:password@code.example/repo",
        "https://user@code.example/repo",
        "https://999.0.0.1/repo",
        "https://code.example/repo\nnext",
        "https://code.example/repo\x00tail",
        "https://code.example/repo\u200btail",
        "https://code.example/a b",
        "https://code.example/a\\b",
        "https://code.example/%0aheader",
    ],
)
def test_unsafe_or_invalid_urls_are_erased_even_if_the_literal_was_sent(url):
    item = code_item(url, url_verified=True)
    block = Block("text", item["quote"], 0)
    result = verify_extraction({"code_data": [item]}, document(block), source(block))["code_data"][
        0
    ]
    assert result["url"] is None and result["url_verified"] is False
    assert "original_url" not in result and "url_alias" not in result


@pytest.mark.parametrize(
    "actual",
    [
        "https://code.example/Repository",
        "https://code.example/Repo/other",
        "https://code.example/Repo-long",
        "https://code.example/Repo.extra",
        "https://code.example/Repo?download=1",
        "https://code.example/Repo#section",
        "https://code.example/Repo%2Fother",
    ],
)
def test_url_must_match_a_full_original_token_rather_than_a_prefix(actual):
    item = code_item("https://code.example/Repo")
    block = Block("text", f"The source is available at {actual}", 0)
    result = verify_extraction({"code_data": [item]}, document(block), source(block))["code_data"][
        0
    ]
    assert result["verified"] is True
    assert result["url"] is None and result["url_verified"] is False


def test_url_path_case_is_sensitive_even_when_general_quote_check_is_casefolded():
    item = code_item("https://code.example/Project")
    block = Block("text", item["quote"].replace("/Project", "/project"), 0)
    result = verify_extraction({"code_data": [item]}, document(block), source(block))["code_data"][
        0
    ]
    assert result["verified"] is True and result["url_verified"] is False and result["url"] is None


@pytest.mark.parametrize("ending", [".", ",", ";", "。", "，", "。下一句", ").", "]", "}"])
def test_normal_prose_terminal_punctuation_does_not_hide_a_complete_url(ending):
    url = "https://code.example/Group/Model"
    item = code_item(url, quote=f"The source is available at {url}{ending}")
    block = Block("text", item["quote"], 0)
    result = verify_extraction({"code_data": [item]}, document(block), source(block))["code_data"][
        0
    ]
    assert result["verified"] is True and result["url_verified"] is True and result["url"] == url


@pytest.mark.parametrize(
    "url",
    [
        "https://code.example/Group/Model(v2)",
        "https://code.example/Group/Model(a(b)c)",
        "https://code.example/Group/Model[release]",
        "https://[::1]/Group/Model(v2)",
    ],
)
def test_markdown_wrapping_preserves_balanced_brackets_inside_the_actual_url(url):
    item = code_item(url, quote=f"The source is available at [repository]({url}).")
    block = Block("text", item["quote"], 0)
    result = verify_extraction({"code_data": [item]}, document(block), source(block))["code_data"][
        0
    ]
    assert result["url"] == url and result["url_verified"] is True


@pytest.mark.parametrize("tail", ["(v2)", "[release]", "/other.", "?download=1.", ".extra."])
def test_punctuation_cleanup_never_accepts_a_shorter_path_query_or_balanced_suffix(tail):
    url = "https://code.example/Repo"
    item = code_item(url)
    block = Block("text", f"The source is available at {url}{tail}", 0)
    result = verify_extraction({"code_data": [item]}, document(block), source(block))["code_data"][
        0
    ]
    assert result["url"] is None and result["url_verified"] is False


def test_url_must_occur_inside_verified_quote_not_only_elsewhere_or_in_header():
    item = code_item(quote=_QUOTE)
    block = Block("text", _QUOTE + "\n" + _URL, 0)
    result = verify_extraction({"code_data": [item]}, document(block), source(block))["code_data"][
        0
    ]
    assert result["verified"] is True and result["url"] is None
    block = Block("text", _QUOTE, 0)
    result = verify_extraction({"code_data": [item]}, document(block), source(block, header=_URL))[
        "code_data"
    ][0]
    assert result["verified"] is True and not result["url_verified"]


def test_url_is_removed_for_wrong_page_unsent_body_or_missing_quote():
    item = code_item()
    block = Block("text", item["quote"], 0)
    for changed, sent in (
        ({**item, "page": 1}, source(block)),
        (item, ExtractionSource(item["quote"], ())),
        ({**item, "quote": None, "page": None}, source(block)),
    ):
        result = verify_extraction({"code_data": [changed]}, document(block), sent)["code_data"][0]
        assert not result["verified"] and not result["url_verified"] and result["url"] is None


def test_highlights_override_forged_model_bbox_with_the_matching_sent_original_block():
    unsent = Block("text", _QUOTE, 0, (1, 2, 3, 4))
    actual = Block("text", _QUOTE, 0, _BOX)
    item = {"kind": "method", "quote": _QUOTE, "page": 0, "bbox": [999, 999, 999, 999]}
    result = verify_extraction({"highlights": [item]}, document(unsent, actual), source(actual))[
        "highlights"
    ][0]
    assert result == {**item, "verified": True, "bbox": _BOX}


def test_cross_block_quotes_can_verify_but_have_no_fabricated_single_box():
    first = Block("text", "The proposed method reduces", 0, (1, 2, 3, 4))
    second = Block("text", "the error on the measured dataset.", 0, _BOX)
    item = {"kind": "result", "quote": _QUOTE, "page": 0, "bbox": _BOX}
    spans = ((0, len(first.text)), (len(first.text) + 1, len(_QUOTE)))
    sent = ExtractionSource(
        _QUOTE, (SentBlock(first, first.text), SentBlock(second, second.text)), spans
    )
    result = verify_extraction({"highlights": [item]}, document(first, second), sent)["highlights"][
        0
    ]
    assert result["verified"] is True and result["bbox"] is None


@pytest.mark.parametrize("header", ["unpaged metadata", _QUOTE])
def test_page_markers_in_actual_message_prevent_splicing_a_quote_between_sent_fragments(header):
    first = Block("text", "The proposed method reduces", 0, (1, 2, 3, 4))
    second = Block("text", "the error on the measured dataset.", 0, _BOX)
    item = {"kind": "result", "quote": _QUOTE, "page": 0}
    result = verify_extraction(
        {"highlights": [item], "claims": [evidence()]},
        document(first, second),
        source(first, second, header=header),
    )
    assert result["highlights"][0]["verified"] is False
    assert result["highlights"][0]["bbox"] is None and not result["claims"][0]["verified"]


def test_sent_block_record_without_quote_in_actual_message_cannot_verify():
    block = Block("text", _QUOTE, 0, _BOX)
    sent = ExtractionSource(
        "[page=0 results]\nOnly a truncated beginning.", (SentBlock(block, _QUOTE),)
    )
    assert not verify_extraction({"problem": evidence()}, document(block), sent)["problem"][
        "verified"
    ]


def test_highlight_without_real_coordinates_or_sent_complete_quote_keeps_no_box():
    for block in (Block("text", _QUOTE, 0), Block("text", _QUOTE, 0, _BOX)):
        sent = (
            source(block)
            if block.bbox is None
            else ExtractionSource(_QUOTE, (SentBlock(block, "The proposed method"),))
        )
        result = verify_extraction(
            {"highlights": [{"kind": "goal", "quote": _QUOTE, "page": 0, "bbox": _BOX}]},
            document(block),
            sent,
        )["highlights"][0]
        assert result["bbox"] is None
        assert result["verified"] is (block.bbox is None)


def test_caption_quote_uses_the_actual_sent_original_figure_coordinates():
    block = Block("figure", "Figure content", 0, _BOX, _QUOTE)
    result = verify_extraction(
        {"highlights": [{"kind": "innovation", "quote": _QUOTE, "page": 0}]},
        document(block),
        source(block),
    )["highlights"][0]
    assert result["verified"] is True and result["bbox"] == _BOX


def test_forged_sent_block_cannot_supply_a_model_invented_page_or_box():
    original = Block("text", _QUOTE, 0, _BOX)
    forged = Block("text", _QUOTE, 0, (900, 900, 999, 999))
    result = verify_extraction(
        {"highlights": [{"kind": "method", "quote": _QUOTE, "page": 0}]},
        document(original),
        source(forged),
    )["highlights"][0]
    assert result["verified"] is False and result["bbox"] is None


def test_preserves_other_fields_and_concepts_without_mutating_input_or_filling_legacy_gaps():
    block = Block("text", _QUOTE, 0)
    data = {
        "summary": "Model summary",
        "innovation": {"text": "Legacy"},
        "key_concepts": [{"name": "Term", "definition": None}],
        "problem": evidence(),
        "claims": [evidence()],
        "questions": [{"question": str(i), "answer": evidence()} for i in range(3)],
    }
    before = deepcopy(data)
    result = verify_extraction(data, document(block), source(block))
    assert data == before and result is not data
    assert (
        result["key_concepts"] == before["key_concepts"]
        and result["innovation"] == before["innovation"]
    )
    assert "method" not in result and "code_data" not in result
    assert "verified" not in data["problem"] and "verified" not in data["questions"][0]["answer"]
    legacy = {"summary": "An old record"}
    assert verify_extraction(legacy, document(block), source(block)) == legacy
