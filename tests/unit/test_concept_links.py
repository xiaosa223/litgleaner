from litgleaner.domain.concept_links import (
    ConceptDecision,
    MergeCandidate,
    canonical_map,
    latest_decisions,
)


def _merge(id_: str, a: str, b: str) -> MergeCandidate:
    return MergeCandidate(id_, a, b, "同一概念的不同写法", "g", "2026-11-14T00:00:00+00:00")


def _decide(target: str, decision: str, at: str) -> ConceptDecision:
    return ConceptDecision(f"d-{target}-{at}", target, decision, at)  # type: ignore[arg-type]


def test_latest_decision_wins_so_users_can_change_their_mind():
    decisions = [
        _decide("m1", "confirmed", "2026-11-14T01:00:00"),
        _decide("m1", "rejected", "2026-11-14T02:00:00"),
        _decide("m2", "confirmed", "2026-11-14T01:00:00"),
    ]
    assert latest_decisions(decisions) == {"m1": "rejected", "m2": "confirmed"}


def test_only_confirmed_merges_map_and_chains_resolve_to_the_end():
    merges = [_merge("m1", "a", "b"), _merge("m2", "b", "c"), _merge("m3", "x", "y")]
    assert canonical_map(merges, {"m1": "confirmed", "m2": "confirmed", "m3": "rejected"}) == {
        "a": "c",
        "b": "c",
    }


def test_cycles_do_not_loop_forever():
    merges = [_merge("m1", "a", "b"), _merge("m2", "b", "a")]
    result = canonical_map(merges, {"m1": "confirmed", "m2": "confirmed"})
    assert set(result) <= {"a", "b"} and all(k != v for k, v in result.items())
