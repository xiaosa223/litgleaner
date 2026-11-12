"""重要前作按全文库分母、不同引用论文及精确目标身份计算。"""

from dataclasses import FrozenInstanceError, replace

import pytest

from litgleaner.domain.citation import Citation
from litgleaner.domain.identity import Alias
from litgleaner.domain.important_works import (
    ImportantWorks,
    project_important_works,
)
from litgleaner.domain.memory import MemoryPaper
from litgleaner.domain.memory_citations import citation_index

NOW = "2026-11-12T00:00:00+00:00"


def _papers(size):
    return {str(i): MemoryPaper(str(i), f"Paper {i}", "read") for i in range(size)}


def _citation(paper_id, *, target="prior", aliases=(), source="parsed", position="0"):
    return Citation(
        paper_id,
        position,
        f"Original reference from {paper_id}, {source}, {position}",
        source,
        7,
        target,
        "Original prior work title",
        aliases,
        (f"Original citation sentence from {paper_id}.",),
        ("methodology",),
        True,
        NOW,
        2,
        (10, 20, 30, 40),
    )


def _project(papers, citations=(), aliases=None):
    index = citation_index(
        citations, aliases or {}, {key: value.title for key, value in papers.items()}
    )
    return project_important_works(papers, index)


@pytest.mark.parametrize("size,threshold", [(1, 1), (2, 1), (3, 1), (4, 2), (6, 2), (7, 3)])
def test_threshold_is_ceiling_of_all_library_papers_including_uncited(size, threshold):
    papers = _papers(size)
    below = tuple(_citation(str(i)) for i in range(threshold - 1))
    assert not _project(papers, below)
    (work,) = _project(papers, (*below, _citation(str(threshold - 1))))
    assert work.citing_count == threshold
    assert len(work.citing_papers) == threshold


def test_empty_library_never_returns_foreign_citations():
    assert not _project({}, (_citation("foreign"),))


def test_uncited_library_returns_no_works():
    assert not _project(_papers(7))


def test_multiple_sources_and_positions_count_once_and_keep_every_original_evidence():
    first = _citation("0")
    second = replace(first, source="remote", position="remote:2", page=4)
    third = replace(first, position="parsed:9", source_position=9, influential=False)
    other = _citation("1")
    (work,) = _project(_papers(4), (first, second, third, other))
    assert work.citing_count == 2
    assert work.citing_papers[0].citations == (first, second, third)
    assert work.citing_papers[1].citations == (other,)
    assert work.citing_papers[0].citations[0].contexts == first.contexts
    assert work.citing_papers[0].citations[0].bbox == first.bbox
    assert work.citing_papers[0].citations[0].source_position == 7
    assert work.citing_papers[0].citations[2].influential is False


def test_duplicate_aliases_and_transitive_identifiers_never_add_votes():
    doi, remote = Alias("doi", "10.1000/prior"), Alias("remote", "prior")
    first = _citation("0", target=None, aliases=(doi, doi))
    second = _citation("0", target=None, aliases=(remote, doi), source="remote")
    third = _citation("1", target=None, aliases=(remote,))
    (work,) = _project(_papers(4), (first, second, third))
    assert work.target.paper_id is None and work.target.aliases == (doi, remote)
    assert work.citing_count == 2
    assert work.citing_papers[0].citations == (first, second)
    assert work.citing_papers[1].citations == (third,)


def test_foreign_citing_papers_do_not_reach_threshold_or_enter_supports():
    library = _papers(4)
    assert not _project(library, (_citation("0"), _citation("foreign")))
    (work,) = _project(library, (_citation("0"), _citation("foreign"), _citation("1")))
    assert work.citing_count == 2
    assert [item.paper.paper_id for item in work.citing_papers] == ["0", "1"]


def test_self_reference_never_casts_a_vote_but_other_papers_still_do():
    papers = _papers(3)
    own = _citation("0", target="0")
    assert not _project(papers, (own,))
    other = _citation("1", target="0")
    (work,) = _project(papers, (own, other))
    assert work.target.paper_id == "0" and work.target.title == "Paper 0"
    assert work.citing_count == 1 and work.citing_papers[0].citations == (other,)


def test_alias_only_self_reference_is_also_excluded():
    alias = Alias("doi", "10.1000/own")
    assert not _project(
        _papers(3), (_citation("0", target=None, aliases=(alias,)),), {"0": (alias,)}
    )


def test_known_outside_target_combines_stored_id_and_dynamic_global_alias():
    alias = Alias("doi", "10.1000/outside")
    first = _citation("0", target="outside")
    second = _citation("1", target=None, aliases=(alias,))
    (work,) = _project(_papers(4), (first, second), {"outside": (alias,)})
    assert work.target.paper_id == "outside" and work.target.aliases == (alias,)
    assert work.citing_count == 2
    assert [item.citations for item in work.citing_papers] == [(first,), (second,)]


def test_global_alias_conflict_cannot_supply_a_vote_to_either_target():
    left, right = Alias("doi", "10.1000/left"), Alias("remote", "right")
    ambiguous = _citation("0", target=None, aliases=(left, right))
    valid = _citation("1", target=None, aliases=(left,))
    aliases = {"left-outside": (left,), "right-outside": (right,)}
    assert not _project(_papers(4), (ambiguous, valid), aliases)


def test_transitive_alias_chain_with_two_known_targets_does_not_create_a_prior():
    left, right = Alias("doi", "10.1000/left"), Alias("doi", "10.1000/right")
    bridge1, bridge2 = Alias("remote", "bridge1"), Alias("remote", "bridge2")
    references = (
        _citation("0", target=None, aliases=(left, bridge1)),
        _citation("1", target=None, aliases=(bridge1, bridge2)),
        _citation("2", target=None, aliases=(bridge2, right)),
    )
    aliases = {"outside-left": (left,), "outside-right": (right,)}
    assert not _project(_papers(3), references, aliases)


def test_stored_target_identity_has_priority_over_conflicting_aliases():
    alias = Alias("doi", "10.1000/wrong")
    first = _citation("0", target="correct", aliases=(alias,))
    second = _citation("1", target="correct")
    (work,) = _project(_papers(4), (first, second), {"wrong": (alias,)})
    assert work.target.paper_id == "correct" and work.citing_count == 2


def test_equal_titles_or_title_only_records_do_not_merge_unrelated_targets():
    first = _citation("0", target=None, aliases=(Alias("doi", "10.1000/one"),))
    second = _citation("1", target=None, aliases=(Alias("doi", "10.1000/two"),))
    title_only = _citation("2", target=None)
    assert first.cited_title == second.cited_title == title_only.cited_title
    assert not _project(_papers(4), (first, second, title_only))


def test_external_identifier_cannot_impersonate_an_internal_paper_id():
    explicit = _citation("0", target="prior")
    external = _citation("1", target=None, aliases=(Alias("paper", "prior"),))
    assert not _project(_papers(4), (explicit, external))


def test_ranking_uses_citing_count_then_exact_target_identity_and_sorted_citers():
    papers = _papers(6)
    citations = (
        _citation("4", target="top"),
        _citation("3", target="b"),
        _citation("2", target="a"),
        _citation("1", target="top"),
        _citation("0", target="b"),
        _citation("0", target="a"),
        _citation("0", target="top"),
    )
    works = _project(dict(reversed(tuple(papers.items()))), citations)
    assert [item.target.paper_id for item in works] == ["top", "a", "b"]
    assert [item.citing_count for item in works] == [3, 2, 2]
    assert [item.paper.paper_id for item in works[0].citing_papers] == ["0", "1", "4"]
    assert _project(papers, tuple(reversed(citations))) == works


def test_original_reading_states_and_titles_are_retained_for_each_citing_paper():
    papers = {
        "0": MemoryPaper("0", "Same author, first paper", "abandoned"),
        "1": MemoryPaper("1", "Same author, second paper", "new"),
        "2": MemoryPaper("2", "Uncited paper", "queued"),
        "3": MemoryPaper("3", "Another uncited paper", "reading"),
    }
    (work,) = _project(papers, (_citation("0"), _citation("1")))
    assert work.citing_count == 2
    assert [support.paper for support in work.citing_papers] == [papers["0"], papers["1"]]


def test_projection_and_response_are_frozen_and_leave_inputs_unchanged():
    papers = _papers(1)
    reference = _citation("0")
    works = _project(papers, (reference,))
    response = ImportantWorks(1, 1, works, 1, 50, 0)
    with pytest.raises(FrozenInstanceError):
        response.threshold = 2
    with pytest.raises(FrozenInstanceError):
        works[0].citing_count = 2
    with pytest.raises(FrozenInstanceError):
        works[0].citing_papers[0].paper = MemoryPaper("other", "Other", "new")
    assert papers == _papers(1) and works[0].citing_papers[0].citations == (reference,)
