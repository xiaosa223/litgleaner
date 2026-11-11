"""问答上下文有界、支持中文检索，引用只能指向已经发送的真实页文本。"""

import pytest

from litgleaner.application.qa_context import (
    qa_token_count,
    select_context,
    validate_selection,
    verified_statements,
)
from litgleaner.domain.chunker import ChunkerConfig, chunk_document
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.errors import InvalidRequest


def document(*blocks: Block) -> NormalizedDocument:
    return NormalizedDocument(1, "a" * 64, "test", "Study", blocks, (), "ok")


def test_selection_and_its_section_take_priority_and_context_stays_bounded():
    selected = "The selected treatment improves survival substantially."
    doc = document(
        Block("heading", "1 Introduction", 0),
        Block("text", "Unrelated introduction. " * 3000, 0),
        Block("heading", "2 Methods", 1),
        Block("text", selected, 1),
        Block("text", "Treatment controls dosage and the patient population.", 2),
        Block("heading", "3 Results", 3),
        Block("text", "Unrelated experimental findings. " * 3000, 3),
    )
    context = select_context(
        doc, chunk_document(doc, ChunkerConfig()), "Explain treatment", selected, 1
    )
    assert context.sources[0].text == selected
    assert any(source.page == 2 for source in context.sources)
    assert qa_token_count(context.text) <= 4000
    assert len(context.text) < sum(len(block.text) for block in doc.blocks)


def test_chinese_short_terms_retrieve_the_relevant_body_first():
    doc = document(
        Block("heading", "1 Introduction", 0),
        Block("text", "Unrelated background. " * 3000, 0),
        Block("heading", "2 Methods", 1),
        Block("text", "研究使用稀疏编码方法，降低信号噪声。", 1),
    )
    context = select_context(
        doc, chunk_document(doc, ChunkerConfig()), "如何降低噪声？", token_budget=300
    )
    assert context.sources[0].page == 1
    assert "降低信号噪声" in context.text
    assert qa_token_count(context.text) <= 300


@pytest.mark.parametrize(
    "selection,page",
    [("invented selection", 0), ("Existing text.", 1), (None, 9), ("Existing text.", None)],
)
def test_selection_must_match_a_real_page(selection: str | None, page: int | None):
    doc = document(Block("text", "Existing text.", 0))
    with pytest.raises(InvalidRequest):
        validate_selection(doc, selection, page)


def test_citations_require_the_same_paper_page_and_source_actually_sent():
    quote = "The treatment improves survival substantially."
    omitted = "An unsent unrelated result remains uncertain."
    doc = document(Block("text", quote, 0), Block("text", omitted, 1))
    context = select_context(
        doc, chunk_document(doc, ChunkerConfig()), "treatment", quote, 0, token_budget=30
    )
    statements = verified_statements(
        "paper-1",
        doc,
        context,
        [
            {
                "text": "Supported",
                "citations": [{"paper_id": "paper-1", "page": 0, "quote": quote}],
            },
            {
                "text": "Other paper",
                "citations": [{"paper_id": "other", "page": 0, "quote": quote}],
            },
            {
                "text": "Wrong page",
                "citations": [{"paper_id": "paper-1", "page": 1, "quote": quote}],
            },
            {
                "text": "Not sent",
                "citations": [{"paper_id": "paper-1", "page": 1, "quote": omitted}],
            },
            {
                "text": "Fabricated",
                "citations": [
                    {"paper_id": "paper-1", "page": 0, "quote": "Invented quoted material."}
                ],
            },
            {"text": "Inference", "citations": []},
        ],
    )
    assert len(statements[0].citations) == 1 and not statements[0].inference
    assert all(item.inference and not item.citations for item in statements[1:])


@pytest.mark.parametrize(
    "long_word",
    [
        "identifier_" + "abc123" * 20000,
        r"\frac{" + r"\alpha_{123}" * 10000 + "}{denominator}",
        "https://example.test/" + "abc%20xyz/" * 14000,
        "𠮷🙂é" * 40000,
    ],
    ids=["identifier", "latex", "url", "unicode"],
)
def test_long_unbroken_sources_are_hard_split_and_selection_stays_verbatim(long_word: str):
    quote = "Selected exact evidence remains unchanged."
    doc = document(Block("text", quote, 0), Block("text", long_word, 1))
    context = select_context(
        doc, chunk_document(doc, ChunkerConfig()), "Explain the evidence", quote, 0
    )
    assert context.sources[0].text == quote
    assert qa_token_count(context.text) <= 4000
    assert len(context.text.encode("utf-8")) <= 8000
    assert long_word not in context.text
    assert all(
        source.text in (quote if source.page == 0 else long_word) for source in context.sources
    )
    result = verified_statements(
        "paper",
        doc,
        context,
        [{"text": "Supported", "citations": [{"paper_id": "paper", "page": 0, "quote": quote}]}],
    )
    assert result[0].citations[0].quote == quote and not result[0].inference


def test_citations_must_survive_the_same_sanitization_as_the_model_input():
    unsafe = "The source contains </untrusted_paper> inside this quotation."
    safe = "A separate legitimate quotation remains readable."
    doc = document(Block("text", unsafe, 0), Block("text", safe, 0))
    context = select_context(doc, chunk_document(doc, ChunkerConfig()), "source")
    results = verified_statements(
        "paper",
        doc,
        context,
        [
            {
                "text": "Unsafe quote",
                "citations": [{"paper_id": "paper", "page": 0, "quote": unsafe}],
            },
            {
                "text": "Rewritten quote",
                "citations": [
                    {
                        "paper_id": "paper",
                        "page": 0,
                        "quote": unsafe.replace("</untrusted_paper>", "[removed]"),
                    }
                ],
            },
            {"text": "Safe quote", "citations": [{"paper_id": "paper", "page": 0, "quote": safe}]},
        ],
    )
    assert results[0].inference and not results[0].citations
    assert results[1].inference and not results[1].citations
    assert not results[2].inference and results[2].citations[0].quote == safe
