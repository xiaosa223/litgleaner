"""发送正文的精确位置与同页连续边界；不借元数据或另一页的相同句子。"""

from dataclasses import replace

import pytest

from litgleaner.application.extraction_facts import verify_extraction
from litgleaner.application.knowledge_ingest import (
    ExtractionSource,
    SentBlock,
    extraction_source,
    sent_page_texts,
)
from litgleaner.domain.chunker import ChunkerConfig, chunk_document
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.paper import Paper

_FIRST = "The proposed method reduces"
_SECOND = "the error on the measured dataset."
_QUOTE = _FIRST + " " + _SECOND


def document(*blocks):
    return NormalizedDocument(1, "a" * 64, "test", "Study", blocks, (), "ok")


def evidence(quote=_QUOTE, page=0):
    return {"problem": {"text": "Model summary", "quote": quote, "page": page}}


def test_constructed_sources_record_exact_raw_offsets_even_with_metadata_and_isolation_tags():
    quote = "The authors use </untrusted_paper> in a purported statement."
    doc = document(
        Block("heading", "Abstract", 0), Block("text", quote, 0), Block("text", _QUOTE, 1)
    )
    paper = Paper("P", _QUOTE, "study", _QUOTE, (), None, None, None, "t")
    sent = extraction_source(paper, doc, chunk_document(doc, ChunkerConfig()), 2000)
    assert sent.spans is not None and len(sent.spans) == len(sent.sources)
    for item, (start, end) in zip(sent.sources, sent.spans, strict=True):
        assert sent.text[start:end] == item.text
    pages = sent_page_texts(sent)
    assert pages[0] == ("The authors use [removed] in a purported statement.",)
    assert pages[1] == (_QUOTE,)
    assert not verify_extraction(evidence(page=0), doc, sent)["problem"]["verified"]


@pytest.mark.parametrize("offsets", [None, ((0, 1),), ((-1, 10),), ((1, 999),)])
def test_metadata_quote_without_actual_body_positions_cannot_be_page_evidence(offsets):
    block = Block("text", _QUOTE, 0)
    sent = ExtractionSource("Metadata: " + _QUOTE, (SentBlock(block, _QUOTE),), offsets)
    assert sent_page_texts(sent) == {}
    assert not verify_extraction(evidence(), document(block), sent)["problem"]["verified"]


@pytest.mark.parametrize("record_offsets", [True, False])
def test_another_page_complete_quote_never_supplies_continuity_for_marker_separated_page(
    record_offsets,
):
    blocks = (Block("text", _FIRST, 0), Block("text", _SECOND, 0), Block("text", _QUOTE, 1))
    text, sources, spans = "Metadata: " + _QUOTE, [], []
    for block in blocks:
        text += f"\n\n[page={block.page} results]\n"
        spans.append((len(text), len(text) + len(block.text)))
        text += block.text
        sources.append(SentBlock(block, block.text))
    sent = ExtractionSource(text, tuple(sources), tuple(spans) if record_offsets else None)
    assert sent_page_texts(sent)[0] == (_FIRST, _SECOND)
    assert not verify_extraction(evidence(page=0), document(*blocks), sent)["problem"]["verified"]
    assert verify_extraction(evidence(page=1), document(*blocks), sent)["problem"]["verified"]


def test_unpositioned_multi_block_messages_require_real_markers_but_single_body_is_safe():
    first, second = Block("text", _FIRST, 0), Block("text", _SECOND, 0)
    sent = ExtractionSource(_QUOTE, (SentBlock(first, _FIRST), SentBlock(second, _SECOND)))
    assert sent_page_texts(sent) == {}
    complete = Block("text", _QUOTE, 0)
    single = ExtractionSource(_QUOTE, (SentBlock(complete, _QUOTE),))
    assert sent_page_texts(single) == {0: (_QUOTE,)}


def test_explicit_contiguous_same_page_spans_preserve_quote_but_cross_page_spans_do_not():
    first, second = Block("text", _FIRST, 0), Block("text", _SECOND, 0)
    spans = ((0, len(_FIRST)), (len(_FIRST) + 1, len(_QUOTE)))
    sent = ExtractionSource(_QUOTE, (SentBlock(first, _FIRST), SentBlock(second, _SECOND)), spans)
    assert sent_page_texts(sent) == {0: (_QUOTE,)}
    other_page = replace(second, page=1)
    changed = replace(sent, sources=(sent.sources[0], SentBlock(other_page, _SECOND)))
    assert sent_page_texts(changed) == {0: (_FIRST,), 1: (_SECOND,)}


def test_explicit_spans_cannot_overlap_or_reorder_body_text():
    block = Block("text", _QUOTE, 0)
    item = SentBlock(block, _QUOTE)
    sent = ExtractionSource(_QUOTE, (item, item), ((0, len(_QUOTE)), (0, len(_QUOTE))))
    assert sent_page_texts(sent) == {}
