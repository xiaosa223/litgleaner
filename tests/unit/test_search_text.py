"""统一搜索字形不改写用户原句；展开、组合和空白不污染原文位置。"""

import pytest

from litgleaner.domain.search_text import _mapped_text, normalize_search_text, search_excerpt


@pytest.mark.parametrize(
    "original,expected",
    [
        ("  ＴＲＡＮＳＦＯＲＭＥＲ\tStraße\n", "transformer strasse"),
        ("Cafe\u0301", "café"),
        ("가", "가"),
        ("研究\u3000方法\n数据", "研究 方法 数据"),
        ("İ", "i\u0307"),
        ("ﬃ", "ffi"),
        ("", ""),
    ],
)
def test_search_normalization_has_one_shared_protocol(original, expected):
    assert normalize_search_text(original) == expected
    assert normalize_search_text(expected) == expected


@pytest.mark.parametrize("word", ["ＳＴＲＡＳＳＥ", "Straße", "Cafe\u0301", "가", "ﬃ"])
@pytest.mark.parametrize("prefix", ["ß " * 500, "Ａ  \t" * 500, "e\u0301 " * 500])
def test_late_match_excerpt_is_continuous_original_text_with_correct_offset(word, prefix):
    original = prefix + "Before " + word + " after. " + "Tail " * 100
    excerpt = search_excerpt(original, [normalize_search_text(word)])
    assert excerpt.startswith("…") and excerpt.endswith("…")
    assert word in excerpt
    assert excerpt[1:-1] in original and len(excerpt[1:-1]) == 320


def test_no_content_match_uses_original_prefix_for_title_only_hit():
    content = "原文ＷＯＲＤ " * 100
    assert search_excerpt(content, ["titleonly"]) == content[:320] + "…"


def test_short_content_is_preserved_without_decorations_or_rewriting():
    content = "  Straße\nCafe\u0301  "
    assert search_excerpt(content, ["strasse"]) == content
    assert search_excerpt("", []) == ""


@pytest.mark.parametrize("cluster", ["L\u0304\u0323", "각", "ß\u0301", "İ", "ﬃ", "\u0345"])
def test_origin_mapping_matches_whole_protocol_even_when_prefix_lengths_shrink(cluster):
    original = " " * 50 + cluster * 100 + "X"
    normalized, origins = _mapped_text(original)
    assert normalized == normalize_search_text(original)
    assert len(origins) == len(normalized)
    assert origins[normalized.index("x")] == len(original) - 1


def test_protocol_is_applied_once_even_when_its_output_is_not_idempotent():
    original = "ß\u0301"
    once = normalize_search_text(original)
    assert once == "ss\u0301"
    assert normalize_search_text(once) == "sś"
    assert _mapped_text(original)[0] == once
