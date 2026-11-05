"""切分器：纯函数、结构优先，真实 IEEE 版式的坑必须覆盖。"""

from litfactory.domain.chunker import ChunkerConfig, chunk_document, estimate_tokens
from litfactory.domain.document import Block, NormalizedDocument
from litfactory.domain.sections import split_sections


def block(kind, text, page=0, caption=None):
    return Block(kind=kind, text=text, page=page, bbox=None, caption=caption)


def doc(*blocks, quality="ok"):
    return NormalizedDocument(
        schema_version=1,
        source_pdf_sha256="a" * 64,
        parser_id="test",
        title="T",
        blocks=tuple(blocks),
        front_matter=(),
        quality=quality,
    )


CFG = ChunkerConfig(target_tokens=50, max_tokens=80, overlap_sentences=1)


def words(n, word="alpha"):
    return " ".join([word] * n) + "."


def test_ieee_run_in_abstract_is_recognized_without_heading():
    sections = split_sections(
        doc(
            block("text", "Author Name"),
            block("text", "Abstract—We propose X."),
            block("text", "Index Terms—x, y"),
            block("heading", "I. INTRODUCTION"),
            block("text", "Intro."),
        )
    )
    types = [s.section_type for s in sections]
    assert types[:2] == ["other", "abstract"]
    assert sections[1].blocks[0].text.startswith("Abstract—")
    assert "Index Terms—x, y" not in [b.text for b in sections[1].blocks]
    assert "introduction" in types


def test_chinese_abstract_run_in():
    sections = split_sections(
        doc(block("text", "摘要：本文提出。"), block("heading", "1 引言"), block("text", "正文"))
    )
    assert sections[0].section_type == "abstract"
    assert sections[1].section_type == "introduction"


def test_nesting_comes_from_numbering_not_text_level():
    sections = split_sections(
        doc(
            block("heading", "I. INTRODUCTION"),
            block("text", "a"),
            block("heading", "II. METHOD"),
            block("heading", "A. Data"),
            block("text", "b"),
            block("heading", "B. Model"),
            block("text", "c"),
            block("heading", "C. Training"),
            block("text", "d"),
            block("heading", "III. CONCLUSION"),
            block("text", "e"),
        )
    )
    by_path = {s.path: s for s in sections}
    assert ("II. METHOD", "A. Data") in by_path
    # “C.” 是小节字母，不能被当成罗马数字 100
    assert ("II. METHOD", "C. Training") in by_path
    assert by_path[("II. METHOD", "A. Data")].section_type == "method"
    assert by_path[("III. CONCLUSION",)].section_type == "conclusion"


def test_letter_i_after_h_is_subsection():
    blocks = [block("heading", "II. METHOD")]
    for letter in "ABCDEFGHI":
        blocks += [block("heading", f"{letter}. Part"), block("text", "x")]
    sections = split_sections(doc(*blocks))
    assert sections[-1].path == ("II. METHOD", "I. Part")


def test_decimal_numbering_nests():
    sections = split_sections(
        doc(
            block("heading", "1. Introduction"),
            block("text", "a"),
            block("heading", "2. Methods"),
            block("heading", "2.1 Data"),
            block("text", "b"),
        )
    )
    assert sections[-1].path == ("2. Methods", "2.1 Data")


def test_unlabeled_sections_between_intro_and_results_are_method():
    sections = split_sections(
        doc(
            block("heading", "I. INTRODUCTION"),
            block("text", "a"),
            block("heading", "II. RANDOM MATRIX THEORY"),
            block("text", "b"),
            block("heading", "III. CASE STUDIES"),
            block("text", "c"),
            block("heading", "IV. CONCLUSION"),
            block("text", "d"),
        )
    )
    assert [s.section_type for s in sections] == [
        "introduction",
        "method",
        "results",
        "conclusion",
    ]


def test_references_and_acknowledgment_do_not_enter_chunks():
    result = chunk_document(
        doc(
            block("heading", "1. Conclusion"),
            block("text", "Keep me."),
            block("heading", "Acknowledgments"),
            block("text", "Thanks funders."),
            block("heading", "References"),
            block("reference", "[1] A ref."),
            block("text", "[2] Text ref."),
        ),
        CFG,
    )
    text = " ".join(c.text for c in result.chunks)
    assert "Keep me." in text
    assert "Thanks" not in text
    assert "ref." not in text
    # 未标记但形如参考文献的条目保留；作者简介这类文字不算
    assert result.references == ("[1] A ref.", "[2] Text ref.")


def test_missing_conclusion_falls_back_and_is_degraded():
    result = chunk_document(
        doc(
            block("heading", "1. Introduction"),
            block("text", "a"),
            block("heading", "2. Alpha"),
            block("text", "b"),
            block("heading", "3. Beta"),
            block("text", "c"),
            block("heading", "References"),
            block("reference", "[1]"),
        ),
        CFG,
    )
    assert result.quality == "degraded"
    assert [c.section_type for c in result.chunks][-2:] == ["conclusion", "conclusion"]
    assert any("结论" in w for w in result.warnings)


def test_input_quality_degraded_is_preserved():
    result = chunk_document(
        doc(block("heading", "Conclusion"), block("text", "x"), quality="degraded"), CFG
    )
    assert result.quality == "degraded"


def test_paragraphs_pack_up_to_target_and_keep_boundaries():
    result = chunk_document(
        doc(
            block("heading", "Conclusion"),
            block("text", words(30, "one")),
            block("text", words(30, "two")),
            block("text", words(5, "three")),
        ),
        CFG,
    )
    assert len(result.chunks) == 2
    assert "two" not in result.chunks[0].text
    assert "two" in result.chunks[1].text
    assert "three" in result.chunks[1].text


def test_long_paragraph_split_by_sentence_with_one_sentence_overlap():
    sentences = [f"Sentence number {i} has quite a few words in it right here." for i in range(30)]
    result = chunk_document(
        doc(block("heading", "Conclusion"), block("text", " ".join(sentences))), CFG
    )
    assert len(result.chunks) > 1
    assert all(c.token_count <= CFG.max_tokens for c in result.chunks)
    first, second = result.chunks[0].text, result.chunks[1].text
    assert first.split(". ")[-1].rstrip(".") in second
    joined = " ".join(c.text for c in result.chunks)
    assert all(s in joined for s in sentences)


def test_giant_sentence_without_punctuation_is_hard_split_including_cjk():
    latin = chunk_document(
        doc(block("heading", "Conclusion"), block("text", " ".join(["w"] * 500))), CFG
    )
    cjk = chunk_document(doc(block("heading", "结论"), block("text", "研究" * 300)), CFG)
    for result in (latin, cjk):
        assert len(result.chunks) > 1
        assert all(c.token_count <= CFG.max_tokens for c in result.chunks)
    assert "".join(c.text for c in cjk.chunks) == "研究" * 300


def test_tables_are_atomic_even_over_limit_and_keep_caption():
    big = "<table>" + "<tr><td>x</td></tr>" * 500 + "</table>"
    result = chunk_document(
        doc(
            block("heading", "Results"),
            block("text", "Before."),
            block("table", big, page=3, caption="TABLE I"),
            block("text", "After."),
        ),
        CFG,
    )
    tables = [c for c in result.chunks if "<table>" in c.text]
    assert len(tables) == 1
    assert tables[0].text.startswith("TABLE I")
    assert tables[0].page_start == 3
    assert big in tables[0].text


def test_equation_stays_in_context_and_is_never_split():
    equation = "$$ " + " ".join(["x"] * 100) + " $$"
    result = chunk_document(
        doc(
            block("heading", "Method"),
            block("text", "We define:"),
            block("equation", "$$ a = b $$"),
            block("text", "Then:"),
            block("equation", equation),
        ),
        CFG,
    )
    assert "We define:" in result.chunks[0].text
    assert "$$ a = b $$" in result.chunks[0].text
    assert any(equation in c.text for c in result.chunks)


def test_figure_without_caption_produces_no_chunk():
    result = chunk_document(
        doc(
            block("heading", "Results"),
            block("figure", ""),
            block("figure", "", caption="Fig. 1. Curve"),
        ),
        CFG,
    )
    assert [c.text for c in result.chunks] == ["Fig. 1. Curve"]


def test_chunking_is_deterministic_and_ids_are_stable():
    source = doc(block("heading", "Conclusion"), block("text", words(200)))
    first, second = chunk_document(source, CFG), chunk_document(source, CFG)
    assert first == second
    assert first.chunks[0].chunk_id.startswith("a" * 16)
    assert len({c.chunk_id for c in first.chunks}) == len(first.chunks)


def test_config_change_changes_version():
    other = ChunkerConfig(target_tokens=60, max_tokens=80, overlap_sentences=1)
    assert other.version != CFG.version


def test_page_range_spans_blocks():
    result = chunk_document(
        doc(
            block("heading", "Conclusion"),
            block("text", "a b c", page=2),
            block("text", "d e f", page=3),
        ),
        CFG,
    )
    assert (result.chunks[0].page_start, result.chunks[0].page_end) == (2, 3)


def test_long_paragraph_split_never_corrupts_numbers_or_urls():
    text = " ".join(
        f"Accuracy reached 95.3% on set {i}, see doi.org/10.1/x and Fig. 3 for details."
        for i in range(30)
    )
    result = chunk_document(doc(block("heading", "Conclusion"), block("text", text)), CFG)
    assert len(result.chunks) > 1
    assert all(c.text in text for c in result.chunks)
    assert "95. 3%" not in " ".join(c.text for c in result.chunks)


def test_long_cjk_paragraph_split_adds_no_spaces():
    text = "这是第一句话，内容比较长一些。" * 60
    result = chunk_document(doc(block("heading", "结论"), block("text", text)), CFG)
    assert len(result.chunks) > 1
    assert all(c.text in text for c in result.chunks)


def test_estimate_tokens_counts_cjk_and_latin():
    assert estimate_tokens("") == 0
    assert estimate_tokens("研究方法") == 4
    assert estimate_tokens("one two three") == 4


def test_spaced_headings_are_classified():
    sections = split_sections(
        doc(
            block("heading", "I. I NTRODUCTION"),
            block("text", "a"),
            block("heading", "V. C ONCLUSION"),
            block("text", "b"),
            block("heading", "5 总 结"),
            block("text", "c"),
            block("heading", "6 结束语"),
            block("text", "d"),
            block("heading", "参　考　文　献"),
            block("text", "[1] ref"),
        )
    )
    assert [s.section_type for s in sections] == [
        "introduction",
        "conclusion",
        "conclusion",
        "conclusion",
        "references",
    ]


def test_chinese_bilingual_front_matter_finds_both_abstracts():
    sections = split_sections(
        doc(
            block("text", "作者甲 作者乙"),
            block("heading", "Survey of Something Important"),
            block("text", "Abstract In recent years things changed."),
            block("text", "Key words a; b"),
            block("text", "摘　要　近年来情况发生了变化。"),
            block("text", "关键词　甲；乙"),
            block("heading", "1 人机边界"),
            block("text", "正文"),
        )
    )
    abstracts = [s for s in sections if s.section_type == "abstract"]
    assert len(abstracts) == 2
    assert abstracts[0].blocks[0].text.startswith("Abstract In")
    assert abstracts[1].blocks[0].text.startswith("摘")
    assert all("Key words" not in b.text for s in abstracts for b in s.blocks)


def test_body_paragraph_starting_with_abstract_word_is_not_an_abstract():
    sections = split_sections(
        doc(
            block("heading", "Related Work"),
            block("text", "Abstract interpretation is a classic technique."),
            block("text", "Abstract syntax trees are used."),
        )
    )
    assert [s.section_type for s in sections] == ["other"]


def test_partially_marked_references_keep_unmarked_entries_but_not_bios():
    result = chunk_document(
        doc(
            block("heading", "1. Conclusion"),
            block("text", "x"),
            block("heading", "References"),
            block("reference", "[1] Marked."),
            block("text", "[2] Unmarked entry."),
            block("text", "3. Numbered entry."),
            block("text", "Author bio, born 1979, professor."),
        ),
        CFG,
    )
    assert result.references == ("[1] Marked.", "[2] Unmarked entry.", "3. Numbered entry.")
