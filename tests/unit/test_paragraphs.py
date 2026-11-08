"""翻译的段落单位：被分页、分栏、图表切开的段落拼回一段，译文再按原文长度切回各块。"""

from litgleaner.domain.document import Block
from litgleaner.domain.paragraphs import paragraphs, slice_to_blocks


def text(value: str, page: int = 1) -> Block:
    return Block("text", value, page)


def test_split_paragraph_is_joined_across_page_and_figure() -> None:
    blocks = (
        text("We propose a model that"),
        Block("figure", "", 1, caption="Figure 1: Overview."),
        text("works well. It is fast.", page=2),
        text("A new paragraph."),
    )
    result = paragraphs(blocks, frozenset({0, 2, 3}))
    assert [p.indices for p in result] == [(0, 2), (3,)]
    assert result[0].text == "We propose a model that works well. It is fast."
    assert result[0].sentences == ("We propose a model that works well.", "It is fast.")


def test_heading_equation_or_finished_sentence_breaks_the_chain() -> None:
    blocks = (
        text("Ends without period"),
        Block("heading", "2 Method", 1),
        text("lowercase start."),
        text("Ends with colon as follows:"),
        Block("equation", "$y = x$", 1),
        text("where x is the input."),
        text("Done."),
        text("next starts lowercase."),
        text("Ends without period"),
        text("Upper start."),
    )
    result = paragraphs(blocks, frozenset({0, 2, 3, 5, 6, 7, 8, 9}))
    assert [p.indices for p in result] == [(0,), (2,), (3,), (5,), (6,), (7,), (8,), (9,)]


def test_chain_is_capped() -> None:
    blocks = tuple(text("and so on") for _ in range(6))
    result = paragraphs(blocks, frozenset(range(6)), max_parts=4)
    assert [p.indices for p in result] == [(0, 1, 2, 3), (4, 5)]


def test_single_block_paragraph_keeps_block_text() -> None:
    (result,) = paragraphs((text("  One. Two.  "),), frozenset({0}))
    assert result.text == "One. Two."
    assert result.sentences == ("One.", "Two.")


def test_slice_whole_sentences_to_their_blocks() -> None:
    blocks = (text("First one. Second starts"), text("here and ends. Third."))
    (paragraph,) = paragraphs(blocks, frozenset({0, 1}))
    assert paragraph.sentences == ("First one.", "Second starts here and ends.", "Third.")
    parts = slice_to_blocks(paragraph, ("第一句。", "第二句从这里开始并结束。", "第三句。"))
    assert parts[0].sources == ("First one.", "Second starts")
    assert parts[1].sources == ("here and ends.", "Third.")
    assert parts[0].sentences[0] == "第一句。"
    assert parts[1].sentences[1] == "第三句。"
    crossing = parts[0].sentences[1] + parts[1].sentences[0]
    assert crossing == "第二句从这里开始并结束。"  # 切开的两半拼回整句
    assert parts[0].sentences[1] and parts[1].sentences[0]


def test_slice_single_block_is_identity() -> None:
    (paragraph,) = paragraphs((text("One. Two."),), frozenset({0}))
    (part,) = slice_to_blocks(paragraph, ("一。", "二。")).values()
    assert part.sources == ("One.", "Two.")
    assert part.sentences == ("一。", "二。")


def test_whitespace_inside_blocks_is_normalized() -> None:
    (result,) = paragraphs((text("One line\nbroken  here. Two."),), frozenset({0}))
    assert result.text == "One line broken here. Two."
    assert "\n" not in "".join(result.sentences)
