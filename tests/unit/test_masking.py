"""翻译前的占位符保护：公式、引用、链接等换成 ⟦n⟧，译后还原并检查缺失、重复和编造。"""

import pytest

from litgleaner.domain.masking import mask, restore


@pytest.mark.parametrize(
    ("text", "protected"),
    [
        ("Let $x_i$ be the input.", ["$x_i$"]),
        ("We use \\alpha{} and \\mathbf{W} here.", ["\\alpha{}", "\\mathbf{W}"]),
        ("Code is at https://github.com/a/b.", ["https://github.com/a/b"]),
        ("See doi:10.1000/xyz123 for data.", ["doi:10.1000/xyz123"]),
        ("Prior work [12] and [1, 3] and [ 6 , 13 ] differ.", ["[12]", "[1, 3]", "[ 6 , 13 ]"]),
        ("As shown in Figure 3 and Table 2.1, see Eq. (4).", ["Figure 3", "Table 2.1", "Eq. (4)"]),
        (
            "Attention helps (Vaswani et al., 2017; Bahdanau and Cho, 2015).",
            ["(Vaswani et al., 2017; Bahdanau and Cho, 2015)"],
        ),
        ("See [the docs](https://x.org/a_(b)) now.", ["[the docs](https://x.org/a_(b))"]),
    ],
)
def test_mask_replaces_fragile_tokens_in_reading_order(text: str, protected: list[str]) -> None:
    masked = mask(text)
    assert [token.original for token in masked.tokens] == protected
    for token in masked.tokens:
        assert token.original not in masked.text
        assert token.placeholder in masked.text
    assert restore(masked.text, masked.tokens).text == text


def test_mask_numbers_continue_from_start() -> None:
    masked = mask("See [3] and [4].", start=5)
    assert [t.placeholder for t in masked.tokens] == ["⟦5⟧", "⟦6⟧"]


def test_plain_text_is_untouched() -> None:
    masked = mask("Attention lets a model focus on relevant parts.")
    assert masked.tokens == ()
    assert masked.text == "Attention lets a model focus on relevant parts."


def test_url_keeps_trailing_period_outside() -> None:
    masked = mask("Visit https://a.org/x.")
    assert masked.tokens[0].original == "https://a.org/x"
    assert masked.text.endswith(".")


def test_restore_tolerates_padded_brackets() -> None:
    masked = mask("Prior work [12] is slow.")
    restored = restore("先前的工作 ⟦ 0 ⟧ 很慢。", masked.tokens)
    assert restored.text == "先前的工作 [12] 很慢。"
    assert restored.problems == ()


def test_restore_reports_missing_duplicate_and_unknown() -> None:
    masked = mask("Prior work [12] uses $x$.")
    assert restore("先前的工作使用 ⟦1⟧。", masked.tokens).problems == ("缺少占位符 ⟦0⟧",)
    assert restore("⟦0⟧ ⟦0⟧ 使用 ⟦1⟧。", masked.tokens).problems == ("占位符 ⟦0⟧ 重复",)
    assert restore("⟦0⟧ 使用 ⟦1⟧ 和 ⟦7⟧。", masked.tokens).problems == ("多出未知占位符 ⟦7⟧",)


def test_batch_marker_is_protected_whole_not_as_a_citation() -> None:
    masked = mask("[[2]] Results [5] improve.")
    assert [t.original for t in masked.tokens] == ["[[2]]", "[5]"]


def test_currency_is_not_math() -> None:
    assert mask("It costs $5 and $10 per run.").tokens == ()
    assert [t.original for t in mask("Let $x$ cost $5.").tokens] == ["$x$"]


def test_batch_markers_and_placeholders_in_source_are_protected() -> None:
    masked = mask("Text with [[3]] and ⟦0⟧ inside.")
    assert [t.original for t in masked.tokens] == ["[[3]]", "⟦0⟧"]
    assert "[[3]]" not in masked.text
