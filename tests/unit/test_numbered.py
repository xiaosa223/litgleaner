"""按句编号分批：每句一个 [[n]]，批内不超过字数上限，段落不拆到两批；流式输出逐行解析。"""

from litgleaner.domain.numbered import LineParser, group_batches, numbered_payload


def test_batches_keep_paragraphs_whole_and_respect_limit() -> None:
    sizes = [100, 300, 250, 900, 50]
    assert group_batches(sizes, limit=500) == ((0, 1), (2,), (3,), (4,))


def test_oversized_paragraph_gets_its_own_batch() -> None:
    assert group_batches([10, 2000, 10], limit=500) == ((0,), (1,), (2,))


def test_payload_numbers_every_sentence() -> None:
    assert numbered_payload(["One.", "Two ⟦0⟧."]) == "[[1]] One.\n[[2]] Two ⟦0⟧."


def test_parser_yields_complete_lines_across_chunks() -> None:
    parser = LineParser()
    assert parser.feed("[[1]] 第一") == []
    assert parser.feed("句。\n[[2]] 第") == [(1, "第一句。")]
    assert parser.feed("二句。") == []
    assert parser.finish() == [(2, "第二句。")]


def test_parser_tolerates_spacing_and_skips_preamble() -> None:
    parser = LineParser()
    lines = parser.feed("好的，以下是译文：\n\n[[ 1 ]]第一句。\n[[2]]  第二句。\n")
    assert lines == [(1, "第一句。"), (2, "第二句。")]
    assert parser.stray == ("好的，以下是译文：",)


def test_parser_keeps_unmarked_lines_for_positional_fallback() -> None:
    parser = LineParser()
    parser.feed("第一句。\n第二句。")
    assert parser.finish() == []
    assert parser.stray == ("第一句。", "第二句。")
