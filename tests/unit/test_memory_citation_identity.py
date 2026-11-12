"""引用最终身份统一控制直接边、共同目标和计票，不丢显式事实。"""

from dataclasses import replace
from itertools import permutations

import pytest

from litgleaner.domain.citation import Citation
from litgleaner.domain.identity import Alias
from litgleaner.domain.important_works import project_important_works
from litgleaner.domain.memory import MemoryPaper
from litgleaner.domain.memory_citations import citation_index
from litgleaner.domain.memory_projection import project_facts

B, OUTSIDE = Alias("doi", "10.1000/b"), Alias("doi", "10.1000/outside")
X, Y = Alias("external", "x"), Alias("external", "y")
OWNERS = {"B": (B,), "outside": (OUTSIDE,)}


def reference(citing, *, target=None, aliases=(), position="0"):
    return Citation(
        citing,
        position,
        "Original reference",
        "parsed",
        0,
        target,
        "Original target title",
        aliases,
        ("Original citation sentence",),
        ("methodology",),
        True,
        "t",
        2,
        (1, 2, 3, 4),
    )


def chain(conflict):
    return (
        reference("A", aliases=(B, X) if conflict else (X,)),
        reference("bridge", aliases=(X, Y)),
        reference("other", aliases=(Y, OUTSIDE if conflict else B)),
    )


@pytest.mark.parametrize("order", tuple(permutations(range(3))))
def test_full_chain_ambiguity_excludes_all_dynamic_edges_in_every_input_order(order):
    originals = chain(True)
    index = citation_index(tuple(originals[i] for i in order), OWNERS, {})
    assert not index.direct and not index.by_paper and not index.targets


@pytest.mark.parametrize("order", tuple(permutations(range(3))))
def test_unique_transitive_chain_resolves_every_original_source_in_every_input_order(order):
    originals = chain(False)
    index = citation_index(tuple(originals[i] for i in order), OWNERS, {})
    assert len(index.targets) == 1
    assert {target.paper_id for target in index.targets.values()} == {"B"}
    assert set(index.direct) == {(item.citing_paper_id, "B") for item in originals}
    for item in originals:
        assert index.direct[item.citing_paper_id, "B"] == (item,)
        assert index.direct[item.citing_paper_id, "B"][0] is item


def test_ambiguous_component_keeps_explicit_ids_as_separate_targets_and_votes():
    dynamic = chain(True)
    first = reference("A", target="B", aliases=(OUTSIDE,), position="explicit")
    second = reference("C", target="B", position="explicit")
    third = reference("D", target="outside", aliases=(B,), position="explicit")
    index = citation_index((*dynamic, first, second, third), OWNERS, {})
    assert index.direct == {("A", "B"): (first,), ("C", "B"): (second,), ("D", "outside"): (third,)}
    assert {target.paper_id for target in index.targets.values()} == {"B", "outside"}
    papers = {id_: MemoryPaper(id_, id_, "read") for id_ in ("A", "B", "C", "D")}
    matches = {match.paper.paper_id: match for match in project_facts("A", papers, index, {})}
    assert matches["B"].cites == (first,)
    (shared,) = matches["C"].common_references
    assert shared.target.paper_id == "B" and shared.here == (first,) and shared.there == (second,)
    (work,) = project_important_works(papers, index)
    assert work.target.paper_id == "B" and work.citing_count == 2
    assert [support.citations for support in work.citing_papers] == [(first,), (second,)]


def test_explicit_record_raw_aliases_never_bridge_unresolved_targets():
    explicit = reference("A", target="B", aliases=(X, Y))
    left, right = reference("C", aliases=(X,)), reference("D", aliases=(Y,))
    index = citation_index((explicit, left, right), {}, {})
    assert index.direct == {("A", "B"): (explicit,)}
    assert len(index.targets) == 3
    assert not set(index.by_paper["C"]) & set(index.by_paper["D"])


def test_resolved_sources_keep_interleaving_duplicates_and_original_objects():
    first = reference("A", aliases=(X,))
    explicit = reference("A", target="B", position="explicit")
    other_source = replace(first, source="remote", position="remote", page=7)
    bridge = reference("C", aliases=(X, B))
    duplicate = replace(first)
    records = (first, explicit, other_source, bridge, duplicate, first)
    index = citation_index(records, OWNERS, {})
    expected = tuple(item for item in records if item.citing_paper_id == "A")
    assert index.direct["A", "B"] == expected
    assert all(
        actual is original
        for actual, original in zip(index.direct["A", "B"], expected, strict=True)
    )
    assert tuple(index.by_paper["A"].values()) == (expected,)


def test_unowned_transitive_chain_is_common_evidence_without_inventing_a_direct_target():
    left, right = reference("A", aliases=(X,)), reference("C", aliases=(X, Y))
    index = citation_index((left, right), {}, {})
    assert not index.direct
    assert len(index.targets) == 1 and next(iter(index.targets.values())).paper_id is None
    assert set(index.by_paper["A"]) == set(index.by_paper["C"])


def test_transitively_resolved_self_citation_does_not_become_own_neighbor_or_vote():
    original, bridge = reference("A", aliases=(X,)), reference("foreign", aliases=(X, B))
    index = citation_index((original, bridge), {"A": (B,)}, {})
    papers = {"A": MemoryPaper("A", "A", "read")}
    assert index.direct["A", "A"] == (original,)
    assert project_facts("A", papers, index, {}) == ()
    assert project_important_works(papers, index) == ()
