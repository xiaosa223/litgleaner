"""概念只来自已发送的真实页，短术语、定义及定位分别核验。"""

from unittest.mock import Mock

import pytest

from litgleaner.application.knowledge_ingest import (
    ExtractionSource,
    extraction_source,
    ingest_concepts,
)
from litgleaner.domain.chunker import ChunkerConfig, chunk_document
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.knowledge import Concept
from litgleaner.domain.paper import Paper


def _document(*blocks: Block) -> NormalizedDocument:
    return NormalizedDocument(1, "a" * 64, "test", "Study", blocks, (), "ok")


def _ingest(document: NormalizedDocument, items: list[dict], *, budget: int = 1000):
    paper = Mock(spec=Paper, title="Study", abstract="Metadata without evidence")
    source = extraction_source(paper, document, chunk_document(document, ChunkerConfig()), budget)
    ctx = Mock()
    ctx.store.knowledge.register.return_value = Concept("concept", "GAN", "now")
    data = ingest_concepts(
        ctx,
        "paper",
        {"key_concepts": items},
        document,
        source,
        input_hash="input",
        producer_version="version",
        now="now",
    )
    return ctx, data, source


def _concept(
    name: str = "GAN", *, definition: str | None = None, page: int = 0, role: str = "core"
) -> dict:
    return {"name": name, "definition": definition, "page": page, "role": role}


def test_true_term_and_definition_use_original_block_bbox():
    definition = "GAN is a generative adversarial network used for synthesis."
    doc = _document(
        Block("heading", "Abstract", 0), Block("text", definition, 0, (10, 20, 300, 400))
    )
    ctx, data, source = _ingest(doc, [_concept(definition=definition)])
    assert "[page=0" in source.text and definition in source.text
    assert data["key_concepts"][0]["verified"]
    ctx.store.knowledge.register.assert_called_once_with("GAN", None, "now")
    occurrence = ctx.store.knowledge.add_occurrence.call_args.args[0]
    assert (occurrence.page, occurrence.bbox, occurrence.definition, occurrence.verified) == (
        0,
        (10, 20, 300, 400),
        definition,
        True,
    )
    assert (occurrence.input_hash, occurrence.producer_version) == ("input", "version")


@pytest.mark.parametrize(
    "name,text",
    [("GAN", "An organ is a biological structure."), ("GAN", "We discuss organGAN here.")],
)
def test_short_ascii_terms_require_word_boundaries(name: str, text: str):
    doc = _document(Block("heading", "Abstract", 0), Block("text", text, 0))
    ctx, data, _ = _ingest(doc, [_concept(name)])
    assert not data["key_concepts"][0]["verified"]
    ctx.store.knowledge.register.assert_not_called()


@pytest.mark.parametrize(
    "definition", [None, "GAN means an invented explanation.", "GAN is unrelated."]
)
def test_unverified_definition_remains_in_extraction_but_never_enters_facts(definition: str | None):
    doc = _document(
        Block("heading", "Abstract", 0), Block("text", "We use GAN for image synthesis.", 0)
    )
    ctx, data, _ = _ingest(doc, [_concept(definition=definition)])
    assert data["key_concepts"][0]["definition"] == definition
    assert data["key_concepts"][0]["verified"] is False
    occurrence = ctx.store.knowledge.add_occurrence.call_args.args[0]
    assert occurrence.definition is None and not occurrence.verified


def test_repeated_concept_prefers_role_then_verified_definition_and_one_position():
    quote = "GAN is a generative adversarial network used for synthesis."
    doc = _document(
        Block("heading", "Abstract", 0),
        Block("text", quote, 0),
        Block("text", "GAN is used again.", 1),
    )
    ctx, _, _ = _ingest(
        doc,
        [
            _concept(role="mentioned"),
            _concept(role="core"),
            _concept("gan", definition=quote, role="core"),
            _concept(page=1, role="prerequisite"),
        ],
    )
    ctx.store.knowledge.register.assert_called_once()
    ctx.store.knowledge.add_occurrence.assert_called_once()
    occurrence = ctx.store.knowledge.add_occurrence.call_args.args[0]
    assert occurrence.role == "core" and occurrence.page == 0 and occurrence.verified


def test_wrong_page_and_unselected_page_never_register_even_if_other_page_has_same_term():
    quote = "GAN is a generative adversarial network used for synthesis."
    doc = _document(
        Block("heading", "Abstract", 0),
        Block("text", quote, 0),
        Block("heading", "2 Methods", 1),
        Block("table", "We use GAN. " * 1000, 1),
    )
    ctx, _, source = _ingest(doc, [_concept(page=1), _concept(page=99)], budget=50)
    assert all(item.page == 0 for item in source.sources)
    ctx.store.knowledge.register.assert_not_called()


def test_isolation_tags_never_verify_definitions_or_terms_removed_before_sending():
    quote = "GAN contains </untrusted_paper> inside its purported definition."
    doc = _document(Block("heading", "Abstract", 0), Block("text", quote, 0))
    ctx, data, _ = _ingest(doc, [_concept(definition=quote), _concept("</untrusted_paper>")])
    assert all(not item["verified"] for item in data["key_concepts"])
    ctx.store.knowledge.register.assert_called_once_with("GAN", None, "now")
    assert ctx.store.knowledge.add_occurrence.call_args.args[0].definition is None


def test_legacy_extraction_without_concepts_remains_unchanged():
    doc = _document(Block("text", "Old extraction contents.", 0))
    ctx = Mock()
    legacy = {"summary": "A legacy extraction."}
    data = ingest_concepts(
        ctx,
        "paper",
        legacy,
        doc,
        ExtractionSource("", ()),
        input_hash="old",
        producer_version="old",
        now="now",
    )
    assert data == legacy
    ctx.store.knowledge.register.assert_not_called()


def test_definition_bbox_uses_real_block_and_separate_markers_do_not_form_a_definition():
    quote = "GAN is a generative adversarial network used for synthesis."
    doc = _document(
        Block("heading", "Abstract", 0),
        Block("text", "We first mention GAN here.", 0, (1, 1, 20, 20)),
        Block("text", quote, 0, (30, 40, 300, 400)),
    )
    ctx, _, _ = _ingest(doc, [_concept(definition=quote)])
    assert ctx.store.knowledge.add_occurrence.call_args.args[0].bbox == (30, 40, 300, 400)
    doc = _document(
        Block("heading", "Abstract", 0),
        Block("text", "GAN is a generative adversarial", 0, (1, 1, 20, 20)),
        Block("text", "network used for synthesis.", 0, (30, 40, 300, 400)),
    )
    ctx, data, source = _ingest(doc, [_concept(definition=quote)])
    occurrence = ctx.store.knowledge.add_occurrence.call_args.args[0]
    assert source.text.count("[page=0 ") >= 2 and quote not in source.text
    assert not occurrence.verified and occurrence.definition is None
    assert not data["key_concepts"][0]["verified"]
    assert occurrence.bbox == (1, 1, 20, 20)  # 只保留真实术语位置


def test_continuous_actual_source_across_blocks_never_invents_a_single_definition_bbox():
    quote = "GAN is a generative adversarial network used for synthesis."
    doc = _document(
        Block("heading", "Abstract", 0),
        Block("text", "GAN is a generative adversarial", 0, (1, 1, 20, 20)),
        Block("text", "network used for synthesis.", 0, (30, 40, 300, 400)),
    )
    ctx, _, source = _ingest(doc, [_concept(definition=quote)])
    first_end = len(doc.blocks[1].text)
    data = ingest_concepts(
        ctx,
        "paper",
        {"key_concepts": [_concept(definition=quote)]},
        doc,
        ExtractionSource(quote, source.sources, ((0, first_end), (first_end + 1, len(quote)))),
        input_hash="input",
        producer_version="version",
        now="now",
    )
    occurrence = ctx.store.knowledge.add_occurrence.call_args.args[0]
    assert data["key_concepts"][0]["verified"]
    assert occurrence.definition == quote and occurrence.bbox is None


@pytest.mark.parametrize("duplicate_at", ["metadata", "other_page"])
def test_definition_cannot_borrow_a_complete_quote_from_metadata_or_another_page(duplicate_at):
    quote = "GAN is a generative adversarial network used for synthesis."
    doc = _document(
        Block("heading", "Abstract", 0),
        Block("text", "GAN is a generative adversarial", 0),
        Block("text", "network used for synthesis.", 0),
        *([Block("text", quote, 1)] if duplicate_at == "other_page" else []),
    )
    ctx, _, source = _ingest(doc, [_concept(definition=quote)])
    if duplicate_at == "metadata":
        text = quote + "\n\n" + source.text
        offset = len(quote) + 2
        source = ExtractionSource(
            text,
            source.sources,
            tuple((start + offset, end + offset) for start, end in source.spans),
        )
    data = ingest_concepts(
        ctx,
        "paper",
        {"key_concepts": [_concept(definition=quote)]},
        doc,
        source,
        input_hash="input",
        producer_version="version",
        now="now",
    )
    assert quote in source.text
    assert not data["key_concepts"][0]["verified"]
    assert ctx.store.knowledge.add_occurrence.call_args.args[0].definition is None


def test_existing_aliases_resolving_to_one_concept_still_keep_the_strongest_occurrence():
    quote = "A GAN is a generative adversarial network used for synthesis."
    doc = _document(Block("heading", "Abstract", 0), Block("text", quote, 0))
    ctx, _, _ = _ingest(
        doc,
        [
            _concept(role="mentioned"),
            _concept("generative adversarial network", definition=quote, role="core"),
        ],
    )
    ctx.store.knowledge.add_occurrence.assert_called_once()
    assert ctx.store.knowledge.add_occurrence.call_args.args[0].role == "core"


def test_multiple_pages_in_one_chunk_are_sent_with_their_true_zero_based_page():
    quote = "GAN is a generative adversarial network used for synthesis."
    doc = _document(
        Block("heading", "Abstract", 0),
        Block("text", "We discuss GAN here.", 0),
        Block("text", quote, 1),
    )
    ctx, data, source = _ingest(doc, [_concept(definition=quote, page=1)])
    assert "[page=0 " in source.text and "[page=1 " in source.text
    assert data["key_concepts"][0]["verified"]
    assert ctx.store.knowledge.add_occurrence.call_args.args[0].page == 1
    ctx, data, _ = _ingest(doc, [_concept(definition=quote, page=0)])
    assert not data["key_concepts"][0]["verified"]
    assert ctx.store.knowledge.add_occurrence.call_args.args[0].definition is None


def test_casefold_expansion_never_turns_a_short_raw_definition_into_a_verified_fact():
    short = "ß" * 6
    doc = _document(Block("heading", "Abstract", 0), Block("text", f"GAN uses {short}.", 0))
    ctx, data, _ = _ingest(doc, [_concept(definition=short)])
    assert not data["key_concepts"][0]["verified"]
    occurrence = ctx.store.knowledge.add_occurrence.call_args.args[0]
    assert occurrence.definition is None and not occurrence.verified


def test_unbroken_block_is_bounded_and_its_unsent_tail_term_is_not_registered():
    long_word = "identifier_" + "abc123" * 20000
    text = long_word + " TailConcept is defined only at the omitted end."
    doc = _document(Block("heading", "Abstract", 0), Block("text", text, 0))
    ctx, data, source = _ingest(doc, [_concept("TailConcept")], budget=200)
    assert len(source.text.encode("utf-8")) <= 400
    assert source.sources and long_word not in source.text
    assert all(item.text in text for item in source.sources)
    assert "TailConcept" not in source.text and not data["key_concepts"][0]["verified"]
    ctx.store.knowledge.register.assert_not_called()


def test_metadata_is_counted_and_truncated_to_preserve_real_body_evidence():
    quote = "GAN is a generative adversarial network used for synthesis."
    doc = _document(Block("heading", "Abstract", 0), Block("text", quote, 0))
    paper = Mock(spec=Paper, title="title_" + "abc123" * 20000, abstract="巨大摘要" * 30000)
    source = extraction_source(paper, doc, chunk_document(doc, ChunkerConfig()), 200)
    assert len(source.text.encode("utf-8")) <= 400
    assert source.sources[0].text == quote and quote in source.text
    ctx = Mock()
    ctx.store.knowledge.register.return_value = Concept("concept", "GAN", "now")
    data = ingest_concepts(
        ctx,
        "paper",
        {"key_concepts": [_concept(definition=quote)]},
        doc,
        source,
        input_hash="hash",
        producer_version="version",
        now="now",
    )
    assert data["key_concepts"][0]["verified"]


@pytest.mark.parametrize("budget", [0, 1, 10, 100])
def test_complete_extraction_source_respects_even_tiny_budgets(budget: int):
    doc = _document(Block("heading", "Abstract", 0), Block("text", "GAN is a study method.", 0))
    paper = Mock(spec=Paper, title="Title " * 5000, abstract="Abstract " * 5000)
    source = extraction_source(paper, doc, chunk_document(doc, ChunkerConfig()), budget)
    assert len(source.text.encode("utf-8")) <= 2 * budget
