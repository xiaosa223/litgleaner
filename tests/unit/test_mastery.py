from litgleaner.domain.mastery import (
    DEFAULT_POLICY,
    MasteryEvent,
    MasteryPolicy,
    compute_mastery,
    is_due,
)

_PROOF = {
    1: ("understand", "missing", "partial"),
    2: ("understand", "correct", "correct"),
    3: ("transfer", "correct", "correct"),
}


def _event(
    kind: str, at: str, *, level: int | None = None, rating: str | None = None
) -> MasteryEvent:
    if kind == "evaluation":
        q, v, o = _PROOF[level or 1]
        return MasteryEvent(f"e-{at}", "c1", "evaluation", None, None, None, at, q, v, o)  # type: ignore[arg-type]
    return MasteryEvent(f"e-{at}", "c1", kind, level, rating, None, at)  # type: ignore[arg-type]


def test_defaults_to_unseen_and_seen_from_reading() -> None:
    assert compute_mastery((), seen=False).level == 0
    state = compute_mastery((), seen=True)
    assert state.level == 1
    assert state.due_at is None


def test_evaluation_raises_level_and_schedules_first_review() -> None:
    state = compute_mastery(
        (_event("evaluation", "2026-11-01T10:00:00+00:00", level=2),), seen=True
    )
    assert state.level == 2
    assert state.due_at == "2026-11-02T10:00:00+00:00"


def test_wrong_answer_does_not_lower_level() -> None:
    events = (
        _event("evaluation", "2026-11-01T10:00:00+00:00", level=2),
        _event("evaluation", "2026-11-02T10:00:00+00:00", level=1),
    )
    assert compute_mastery(events, seen=True).level == 2


def test_reading_alone_never_exceeds_seen() -> None:
    events = (_event("evaluation", "2026-11-01T10:00:00+00:00", level=1),)
    state = compute_mastery(events, seen=True)
    assert state.level == 1
    assert state.due_at is None


def test_override_wins_over_earlier_events_and_can_lower() -> None:
    events = (
        _event("evaluation", "2026-11-01T10:00:00+00:00", level=3),
        _event("override", "2026-11-02T10:00:00+00:00", level=1),
    )
    state = compute_mastery(events, seen=True)
    assert state.level == 1
    assert state.overridden
    assert state.due_at is None


def test_review_ratings_move_interval() -> None:
    base = _event("evaluation", "2026-11-01T00:00:00+00:00", level=2)
    remembered = _event("review", "2026-11-02T00:00:00+00:00", rating="remembered")
    state = compute_mastery((base, remembered), seen=True)
    assert state.due_at == "2026-11-05T00:00:00+00:00"  # 第二档 3 天
    forgot = _event("review", "2026-11-05T00:00:00+00:00", rating="forgot")
    state = compute_mastery((base, remembered, forgot), seen=True)
    assert state.due_at == "2026-11-06T00:00:00+00:00"  # 回到第一档 1 天
    assert state.level == 2


def test_repeated_remembering_reaches_apply() -> None:
    policy = MasteryPolicy(intervals=(1, 3), remembers_to_apply=2)
    events = (
        _event("evaluation", "2026-11-01T00:00:00+00:00", level=2),
        _event("review", "2026-11-02T00:00:00+00:00", rating="remembered"),
        _event("review", "2026-11-05T00:00:00+00:00", rating="fuzzy"),
        _event("review", "2026-11-08T00:00:00+00:00", rating="remembered"),
    )
    state = compute_mastery(events, seen=True, policy=policy)
    assert state.level == 3
    assert state.due_at == "2026-11-11T00:00:00+00:00"  # 最后一档封顶


def test_events_are_applied_in_time_order() -> None:
    events = (
        _event("override", "2026-11-03T00:00:00+00:00", level=0),
        _event("evaluation", "2026-11-01T00:00:00+00:00", level=2),
    )
    assert compute_mastery(events, seen=True).level == 0


def test_is_due_and_policy_hash_stable() -> None:
    state = compute_mastery(
        (_event("evaluation", "2026-11-01T00:00:00+00:00", level=2),), seen=True
    )
    assert not is_due(state, "2026-11-01T23:59:59+00:00")
    assert is_due(state, "2026-11-02T00:00:00+00:00")
    assert DEFAULT_POLICY.policy_hash == MasteryPolicy().policy_hash
    assert MasteryPolicy(intervals=(2,)).policy_hash != DEFAULT_POLICY.policy_hash
