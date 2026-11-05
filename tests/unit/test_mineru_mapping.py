"""用真实 MinerU 输出（arXiv:1907.10485v1）验证归一化和切分。"""

import json
from pathlib import Path

import pytest

from litfactory.adapters.parsers.mineru_content_list import to_normalized_document
from litfactory.domain.chunker import ChunkerConfig, chunk_document
from litfactory.domain.errors import SchemaChanged

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mineru" / "arxiv-1907.10485v1.content_list.json"


@pytest.fixture(scope="module")
def items():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def document(items):
    return to_normalized_document(items, pdf_sha256="f" * 64, parser_id="mineru_cloud@vlm")


def kinds(document, kind):
    return [b for b in document.blocks if b.kind == kind]


def test_counts_match_the_verified_experiment(document):
    assert len(kinds(document, "equation")) == 11
    assert len(kinds(document, "table")) == 5
    assert len(kinds(document, "figure")) == 26
    assert len(kinds(document, "reference")) == 24
    assert document.quality == "ok"


def test_title_comes_from_first_level_one_heading(document):
    assert (
        document.title == "Early Anomaly Detection in Power Systems Based on Random Matrix Theory"
    )
    assert document.title not in [b.text for b in kinds(document, "heading")]


def test_noise_is_dropped_and_footnotes_become_front_matter(document):
    texts = " ".join(b.text for b in document.blocks)
    assert "arXiv:1907.10485v1 [eess.SP]" not in texts
    assert len(document.front_matter) == 3
    assert any("National Key" in f for f in document.front_matter)


def test_pages_and_bbox_are_preserved(document):
    assert {b.page for b in document.blocks} == set(range(8))
    assert all(len(b.bbox) == 4 for b in document.blocks if b.bbox)


def test_table_keeps_html_and_caption(document):
    table = kinds(document, "table")[0]
    assert "<table>" in table.text
    assert "TABLE I" in table.caption


def test_real_paper_sections_and_chunks(document):
    result = chunk_document(document, ChunkerConfig())
    by_type = {}
    for chunk in result.chunks:
        by_type.setdefault(chunk.section_type, []).append(chunk)
    assert by_type["abstract"][0].text.startswith("Abstract—")
    assert by_type["conclusion"][0].text.startswith("Based on random matrix theory")
    assert {"introduction", "method", "results"} <= set(by_type)
    assert len(result.references) == 24
    assert result.quality == "ok"
    limit = ChunkerConfig().max_tokens
    oversized = [c for c in result.chunks if c.token_count > limit]
    assert all("<table" in c.text for c in oversized)
    assert sorted({c.page_start for c in result.chunks})[0] == 0


def test_case_study_subsections_inherit_results(document):
    result = chunk_document(document, ChunkerConfig())
    case = [
        c
        for c in result.chunks
        if c.section_path and c.section_path[-1].startswith("C. Case Study")
    ]
    assert case
    assert {c.section_type for c in case} == {"results"}
    approach = [
        c
        for c in result.chunks
        if c.section_path and "Anomaly Detection Approach" in c.section_path[-1]
    ]
    assert {c.section_type for c in approach} == {"method"}


def test_unknown_type_keeps_text_and_marks_degraded():
    document = to_normalized_document(
        [{"type": "mystery", "text": "keep me", "page_idx": 0}],
        pdf_sha256="f" * 64,
        parser_id="x",
    )
    assert document.quality == "degraded"
    assert document.blocks[0].text == "keep me"


@pytest.mark.parametrize("item", [{"text": "no type", "page_idx": 0}, {"type": "text"}, "nope"])
def test_malformed_item_raises_schema_changed(item):
    with pytest.raises(SchemaChanged):
        to_normalized_document([item], pdf_sha256="f" * 64, parser_id="x")


def test_non_list_payload_raises_schema_changed():
    with pytest.raises(SchemaChanged):
        to_normalized_document({"oops": 1}, pdf_sha256="f" * 64, parser_id="x")


def load(name):
    path = FIXTURE.parent / f"{name}.content_list.min.json"
    return to_normalized_document(
        json.loads(path.read_text(encoding="utf-8")), pdf_sha256="e" * 64, parser_id="m"
    )


def test_headers_footers_dropped_and_images_become_figures():
    document = to_normalized_document(
        [
            {"type": "header", "text": "期刊名", "page_idx": 0},
            {"type": "footer", "text": "版权", "page_idx": 0},
            {"type": "image", "image_caption": ["Fig. 1 流程"], "page_idx": 0, "content": ""},
        ],
        pdf_sha256="f" * 64,
        parser_id="x",
    )
    assert document.quality == "ok"
    assert [(b.kind, b.caption) for b in document.blocks] == [("figure", "Fig. 1 流程")]


@pytest.mark.parametrize("name", ["arxiv-1706.03762v7", "arxiv-1602.04938v3", "crad-202220410"])
def test_real_layouts_map_without_unknown_types(name):
    assert load(name).quality == "ok"


def test_chinese_journal_layout_finds_abstract_and_conclusion():
    result = chunk_document(load("crad-202220410"), ChunkerConfig())
    by_type = {c.section_type for c in result.chunks}
    assert {"abstract", "conclusion", "limitation"} <= by_type
    assert result.quality == "ok"
    assert len(result.references) == 82
    abstracts = [c.text for c in result.chunks if c.section_type == "abstract"]
    assert any(text.startswith("摘") for text in abstracts)


@pytest.mark.parametrize("name", ["arxiv-1706.03762v7", "arxiv-1602.04938v3"])
def test_single_column_layouts_have_full_structure(name):
    result = chunk_document(load(name), ChunkerConfig())
    types = {c.section_type for c in result.chunks}
    assert {"abstract", "introduction", "method", "results", "conclusion"} <= types
    assert result.quality == "ok"
