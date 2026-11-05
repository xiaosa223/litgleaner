"""直接从 PDF 文字层提取：作为对照组，也是 MinerU 不可用时的降级方案。"""

import pytest
from tests.helpers.pdf import make_pdf

import litfactory.adapters.parsers.pdf_text as module
from litfactory.adapters.parsers.pdf_text import PdfTextParser
from litfactory.domain.chunker import ChunkerConfig, chunk_document
from litfactory.domain.errors import PermanentError

LONG = "This sentence is long enough to look like a normal line of body text in a paper."


def parse(pages):
    return PdfTextParser().parse(make_pdf(pages), "c" * 64).document


def test_extracts_headings_paragraphs_and_pages():
    document = parse(
        [
            [
                "Abstract-We study things and report results here.",
                "I. INTRODUCTION",
                LONG,
                LONG,
                "End.",
            ],
            [
                "II. CONCLUSION",
                LONG,
                "We found it works.",
                "REFERENCES",
                "[1] A. Author, Title, 2020.",
            ],
        ]
    )
    headings = [b.text for b in document.blocks if b.kind == "heading"]
    assert headings == ["I. INTRODUCTION", "II. CONCLUSION", "REFERENCES"]
    assert {b.page for b in document.blocks} == {0, 1}
    assert document.parser_id == "pdf_text@pypdf"
    result = chunk_document(document, ChunkerConfig())
    types = {c.section_type for c in result.chunks}
    assert {"abstract", "introduction", "conclusion"} <= types
    assert result.references == ("[1] A. Author, Title, 2020.",)


def test_joins_hyphenated_line_breaks():
    document = parse([["1. Conclusion", "The experi-", "ment works well in practice."]])
    text = " ".join(b.text for b in document.blocks if b.kind == "text")
    assert "experiment works" in text


def test_sentence_heading_is_not_mistaken_for_section():
    document = parse([["1. Introduction", "2. We then describe the method in detail here."]])
    assert [b.text for b in document.blocks if b.kind == "heading"] == ["1. Introduction"]


def test_scanned_pdf_without_text_is_degraded():
    document = parse([[]])
    assert document.quality == "degraded"


def test_corrupt_pdf_is_permanent_error():
    with pytest.raises(PermanentError):
        PdfTextParser().parse(b"%PDF-1.4 not really", "c" * 64)


def test_raw_is_none_because_input_pdf_is_already_stored():
    result = PdfTextParser().parse(make_pdf([["1. Conclusion", LONG]]), "c" * 64)
    assert result.raw is None


def test_run_in_abstract_starts_new_block_even_after_title_lines():
    title = ["Early Detection Of Things", "Author One, Author Two"]
    document = parse([[*title, "Abstract-We detect things.", LONG]])
    texts = [b.text for b in document.blocks if b.kind == "text"]
    assert any(text.startswith("Abstract-We detect things.") for text in texts)


def test_page_headers_and_years_are_not_headings():
    header = "2262 Journal of Computer Research 2024, 61(9)"
    document = parse([[header, "2003. In recent years, more things", "1 Introduction", LONG]])
    assert [b.text for b in document.blocks if b.kind == "heading"] == ["1 Introduction"]


def test_any_reader_crash_is_permanent(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RecursionError("hostile pdf")

    monkeypatch.setattr(module, "PdfReader", explode)
    with pytest.raises(PermanentError, match="RecursionError"):
        PdfTextParser().parse(b"%PDF-1.4", "c" * 64)


def test_page_limit_is_enforced():
    with pytest.raises(PermanentError, match="页"):
        PdfTextParser(max_pages=1).parse(make_pdf([["a"], ["b"]]), "c" * 64)
