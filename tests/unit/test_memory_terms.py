"""候选查询只从有限元数据构造字面检索词。"""

import pytest

from litgleaner.domain.memory_terms import candidate_terms


def test_terms_preserve_first_occurrence_across_title_and_abstract():
    assert candidate_terms("Graph learning for graphs", "Learning Graph models") == (
        "graph",
        "learning",
        "graphs",
        "models",
    )


def test_unicode_case_and_whitespace_are_normalized_before_deduplication():
    assert candidate_terms("ＧＲＡＰＨ\tStraße", "graph\nSTRASSE café") == (
        "graph",
        "strasse",
        "café",
    )


def test_cjk_runs_supply_overlapping_three_character_windows():
    assert candidate_terms("神经网络模型", "网络模型 神经") == (
        "神经网",
        "经网络",
        "网络模",
        "络模型",
    )
    assert candidate_terms("あいうえ", "가나다라") == ("あいう", "いうえ", "가나다", "나다라")


@pytest.mark.parametrize(
    "title,abstract,expected",
    [
        ("Transformer模型研究", None, ("transformer", "模型研", "型研究")),
        (
            "卷积Transformer网络",
            "BERT神经网络 attentionテキストモデル CNN가나다라",
            (
                "transformer",
                "bert",
                "神经网",
                "经网络",
                "attention",
                "テキス",
                "キスト",
                "ストモ",
                "トモデ",
                "モデル",
                "cnn",
                "가나다",
                "나다라",
            ),
        ),
    ],
)
def test_mixed_scripts_keep_plain_words_and_bound_cjk_windows(title, abstract, expected):
    assert candidate_terms(title, abstract) == expected


def test_operators_and_punctuation_are_data_and_cannot_reach_fts_as_syntax():
    terms = candidate_terms('Graph" OR (Neural*) -NOT near:3', "_word_ [other]")
    assert terms == ("graph", "neural", "not", "near", "_word_", "other")
    assert all(term.replace("_", "").isalnum() for term in terms)


def test_empty_short_and_common_english_words_supply_no_query():
    assert candidate_terms("AI to be or not", None) == ("not",)
    assert candidate_terms("我 AI 12", "we the and for with from") == ()
    assert candidate_terms("", None) == ()


def test_input_bound_is_applied_before_normalization_and_abstract_processing():
    assert candidate_terms(" " * 8192, "Should not be included") == ()
    assert candidate_terms("a" * 8192 + " after", None) == ()
    assert candidate_terms("graph", " " * 8192 + " excluded") == ("graph",)


def test_long_plain_words_are_ignored_and_query_size_is_bounded():
    assert candidate_terms("a" * 65 + " Graph " + "b" * 64, None) == ("graph", "b" * 64)
    title = " ".join(f"term{index}" for index in range(80))
    assert candidate_terms(title, None) == tuple(f"term{index}" for index in range(32))
    assert candidate_terms(title, None, 2) == ("term0", "term1")
    assert len(candidate_terms(title, None, 64)) == 64


@pytest.mark.parametrize("limit", [0, 65, -1, True, 1.5, "3", None])
def test_invalid_limits_are_rejected(limit):
    with pytest.raises(ValueError):
        candidate_terms("Graph neural networks", None, limit)


@pytest.mark.parametrize("title,abstract", [(None, None), (12, None), ("Graph", []), ("Graph", 1)])
def test_invalid_metadata_types_are_rejected(title, abstract):
    with pytest.raises(ValueError):
        candidate_terms(title, abstract)
