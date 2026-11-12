"""纯信号融合的数值、去重和确定顺序合同。"""

from itertools import permutations

import pytest

from litgleaner.domain.search_hybrid import SearchSignal, fuse


def signal(id_, *, kind="paper", paper="p", bm25=None, cosine=None, input_hash=None):
    return SearchSignal(kind, paper, id_, bm25, cosine, input_hash)


def test_text_normalization_and_positive_cosine_have_equal_weight():
    rows = fuse((signal("a", bm25=-4, cosine=0.25), signal("b", bm25=-2, cosine=0.75)))
    assert [row.id for row in rows] == ["a", "b"]
    assert rows[0].text_contribution == 1 and rows[0].cosine_contribution == 0.25
    assert rows[1].text_contribution == 0.5 and rows[1].total == 1.25


def test_zero_bm25_short_terms_receive_one_point_but_semantic_only_does_not():
    rows = {row.id: row for row in fuse((signal("a", bm25=0), signal("b", cosine=0.5)))}
    assert rows["a"].text_contribution == 1
    assert rows["b"].text_contribution == 0 and rows["b"].total == 0.5


def test_negative_cosine_keeps_raw_signal_with_zero_contribution():
    (row,) = fuse((signal("a", bm25=-2, cosine=-0.8, input_hash="current"),))
    assert row.cosine == -0.8 and row.cosine_contribution == 0
    assert row.total == 1 and row.input_hash == "current"


def test_duplicate_identity_uses_strongest_independent_signals_without_accumulating():
    rows = fuse(
        (
            signal("a", bm25=-1, cosine=0.9, input_hash="one"),
            signal("a", bm25=-3, cosine=0.2, input_hash="two"),
        )
    )
    assert len(rows) == 1
    assert (rows[0].bm25, rows[0].cosine, rows[0].total, rows[0].input_hash) == (
        -3,
        0.9,
        1.9,
        "one",
    )


def test_same_id_in_other_paper_or_source_remains_independent():
    rows = fuse(
        (
            signal("a", bm25=-1),
            signal("a", kind="note", bm25=-1),
            signal("a", paper="other", bm25=-1),
        )
    )
    assert len(rows) == 3
    assert [(row.kind, row.paper_id) for row in rows] == [
        ("note", "p"),
        ("paper", "other"),
        ("paper", "p"),
    ]


def test_all_input_orders_have_identical_ranks_and_duplicate_hash_choice():
    inputs = (
        signal("b", bm25=-1, cosine=0.5, input_hash="a"),
        signal("b", bm25=-1, cosine=0.5, input_hash="z"),
        signal("a", bm25=-1, cosine=0.5),
    )
    expected = fuse(inputs)
    assert expected[-1].input_hash == "z"
    assert all(fuse(order) == expected for order in permutations(inputs))


@pytest.mark.parametrize(
    "bm25,cosine",
    [
        (float("nan"), None),
        (float("inf"), None),
        (1.0, None),
        (True, None),
        (None, float("nan")),
        (None, float("inf")),
        (None, 1.01),
        (None, -1.01),
        (None, True),
    ],
)
def test_invalid_signal_cannot_poison_rank_order(bm25, cosine):
    with pytest.raises(ValueError):
        fuse((signal("a", bm25=bm25, cosine=cosine),))


def test_empty_signal_pool_has_no_rank():
    assert fuse(()) == ()
