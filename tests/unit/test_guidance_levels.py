from litgleaner.domain.guidance import treatment_for
from litgleaner.domain.mastery import evaluation_level


def test_understand_correct_reaches_explain() -> None:
    assert evaluation_level("understand", "correct", "correct") == 2
    assert evaluation_level("understand", "correct", "partial") == 2


def test_apply_needs_whole_compare_or_transfer_answer_correct() -> None:
    assert evaluation_level("compare", "correct", "correct") == 3
    assert evaluation_level("transfer", "correct", "correct") == 3
    assert evaluation_level("compare", "correct", "partial") == 2
    assert evaluation_level("transfer", "correct", "incorrect") == 2


def test_missing_or_misunderstood_never_raise_above_seen() -> None:
    for verdict in ("missing", "misunderstood"):
        assert evaluation_level("understand", verdict, "correct") == 1  # type: ignore[arg-type]


def test_treatment_by_level() -> None:
    assert [treatment_for(n) for n in range(4)] == ["full", "remind", "brief", "brief"]
