"""文库关系只能来自精确事实；重复来源不增加共同目标数。"""

from dataclasses import replace

from litgleaner.domain.citation import Citation
from litgleaner.domain.identity import Alias
from litgleaner.domain.knowledge import Concept, ConceptOccurrence
from litgleaner.domain.memory import FactSignals, FieldEvidence, MemoryPaper
from litgleaner.domain.memory_citations import citation_index
from litgleaner.domain.memory_projection import project_facts

NOW = "2026-11-12T00:00:00+00:00"
PAPERS = {key: MemoryPaper(key, f"Paper {key}", "read") for key in ("a", "b", "c", "d")}


def _citation(
    paper: str,
    *,
    target: str | None = None,
    aliases: tuple[Alias, ...] = (),
    source: str = "parsed",
    position: str = "0",
    title: str | None = None,
) -> Citation:
    return Citation(
        paper,
        position,
        f"Original citation from {paper} ({source}, {position})",
        source,
        0,
        target,
        title,
        aliases,
        (f"Original citation context from {paper}",),
        ("methodology",),
        True,
        NOW,
        page=2,
        bbox=(10, 20, 30, 40),
    )


def _occurrence(paper: str, concept_id: str = "concept") -> ConceptOccurrence:
    return ConceptOccurrence(
        concept_id, paper, "core", None, 3, (10, 20, 30, 40), False, "input", "producer", NOW
    )


def _field(text: str, quote: str, page: int, extraction_id: int = 1) -> FieldEvidence:
    return FieldEvidence(text, quote, page, extraction_id, f"input-{extraction_id}", "producer")


def _facts(citations=(), aliases=None, signals=None, paper="a", papers=None):
    library = papers or PAPERS
    index = citation_index(
        citations, aliases or {}, {key: value.title for key, value in library.items()}
    )
    return project_facts(paper, library, index, signals or {})


def test_direct_citations_keep_direction_and_all_source_evidence():
    forward = _citation("a", target="b")
    other_source = replace(forward, source="source", position="remote:1")
    reverse = _citation("b", target="a")
    (match,) = _facts((forward, other_source, reverse))
    assert match.paper == PAPERS["b"]
    assert match.cites == (forward, other_source)
    assert match.cited_by == (reverse,)
    assert match.cites[0].contexts == ("Original citation context from a",)
    assert match.cites[0].page == 2 and match.cites[0].bbox == (10, 20, 30, 40)
    assert not match.common_references


def test_dynamic_alias_resolution_uses_global_ownership_and_target_can_arrive_later():
    alias = Alias("doi", "10.1000/b")
    reference = _citation("a", aliases=(alias,))
    assert not _facts((reference,))
    (match,) = _facts((reference,), {"b": (alias,)})
    assert match.paper.paper_id == "b" and match.cites == (reference,)


def test_self_citation_never_becomes_a_neighbor():
    assert not _facts((_citation("a", target="a"),))


def test_candidates_outside_library_never_become_neighbors():
    assert not _facts((_citation("a", target="candidate"),))


def test_common_reference_is_one_target_with_each_original_source_preserved():
    doi, remote = Alias("doi", "10.1000/target"), Alias("remote", "target")
    first = _citation("a", aliases=(doi,))
    second = _citation("a", aliases=(remote, doi), source="source", position="source:1")
    third = _citation("b", aliases=(remote,))
    (match,) = _facts((first, second, third))
    (common,) = match.common_references
    assert common.here == (first, second) and common.there == (third,)
    assert common.target.paper_id is None
    assert common.target.aliases == (doi, remote)
    assert not match.cites and not match.cited_by


def test_multiple_shared_targets_are_distinct_and_ordered_by_canonical_identity():
    doi1, doi2 = Alias("doi", "10.1000/1"), Alias("doi", "10.1000/2")
    refs = tuple(
        _citation(paper, aliases=(alias,), position=alias.value)
        for alias in (doi2, doi1)
        for paper in ("b", "a")
    )
    (match,) = _facts(refs)
    assert [common.target.aliases for common in match.common_references] == [(doi1,), (doi2,)]


def test_known_target_unifies_explicit_and_alias_only_citations():
    alias = Alias("doi", "10.1000/known")
    first, second = _citation("a", target="outside"), _citation("b", aliases=(alias,))
    (match,) = _facts((first, second), {"outside": (alias,)})
    (common,) = match.common_references
    assert common.target.paper_id == "outside"
    assert common.here == (first,) and common.there == (second,)


def test_equal_titles_without_intersecting_identifiers_do_not_link_targets():
    first = _citation("a", aliases=(Alias("doi", "10.1000/one"),), title="Same title")
    second = _citation("b", aliases=(Alias("doi", "10.1000/two"),), title="Same title")
    assert not _facts((first, second))


def test_unidentified_title_only_references_do_not_generate_common_targets():
    assert not _facts(
        (_citation("a", title="Identical title"), _citation("b", title="Identical title"))
    )


def test_library_and_outside_alias_conflict_cannot_become_a_false_unique_match():
    local, outside = Alias("doi", "10.1000/b"), Alias("remote", "outside")
    ambiguous = _citation("a", aliases=(local, outside))
    common_other = _citation("c", aliases=(local,))
    aliases = {"b": (local,), "not-in-library": (outside,)}
    assert not _facts((ambiguous, common_other), aliases)


def test_transitive_alias_chain_with_two_known_targets_is_not_a_common_reference():
    left = Alias("doi", "10.1000/left")
    right = Alias("doi", "10.1000/right")
    bridge1, bridge2 = Alias("remote", "bridge1"), Alias("remote", "bridge2")
    references = (
        _citation("a", aliases=(left, bridge1)),
        _citation("b", aliases=(bridge1, bridge2)),
        _citation("c", aliases=(bridge2, right)),
    )
    matches = _facts(references, {"outside-left": (left,), "outside-right": (right,)})
    assert not matches


def test_explicit_stored_target_has_priority_over_conflicting_aliases():
    wrong, outside = Alias("doi", "10.1000/c"), Alias("remote", "outside")
    reference = _citation("a", target="b", aliases=(wrong, outside))
    (match,) = _facts((reference,), {"c": (wrong,), "outside": (outside,)})
    assert match.paper.paper_id == "b" and match.cites == (reference,)


def test_external_alias_namespace_cannot_impersonate_an_internal_paper_id():
    reference = _citation("a", aliases=(Alias("paper", "b"),))
    assert not _facts((reference,))


def test_internal_and_external_targets_with_same_spelling_remain_distinct():
    explicit = _citation("a", target="b")
    external = _citation("c", aliases=(Alias("paper", "b"),))
    (match,) = _facts((explicit, external))
    assert match.paper.paper_id == "b" and match.cites == (explicit,)
    assert not match.common_references


def test_neighbors_are_stably_sorted_and_unrelated_papers_are_omitted():
    citations = (_citation("a", target="d"), _citation("c", target="a"), _citation("a", target="b"))
    matches = _facts(citations, papers={key: PAPERS[key] for key in ("d", "c", "b", "a")})
    assert [match.paper.paper_id for match in matches] == ["b", "c", "d"]


def test_shared_core_concept_keeps_each_occurrence_including_unverified_definition_state():
    concept = Concept("concept", "Transformer", NOW)
    first, second = _occurrence("a"), _occurrence("b")
    signals = {
        "a": FactSignals(core_concepts=((concept, first),)),
        "b": FactSignals(core_concepts=((concept, second),)),
        "c": FactSignals(
            core_concepts=((replace(concept, id="other"), _occurrence("c", "other")),)
        ),
    }
    (match,) = _facts(signals=signals)
    (common,) = match.core_concepts
    assert common.concept_id == concept.id and common.name == "Transformer"
    assert common.here == first and common.there == second
    assert common.here.definition is None and not common.here.verified


def test_shared_fields_normalize_unicode_case_and_whitespace_and_keep_every_evidence():
    first = _field("ＭＮＩＳＴ  Dataset", "Original dataset quotation from A", 0)
    second = _field("mnist\t dataset", "Another original dataset quotation from A", 2)
    third = _field("Mnist DATASET", "Original dataset quotation from B", 4, extraction_id=2)
    method1 = _field(" Attention   Network ", "Original method quotation from A", 1)
    method2 = _field("attention network", "Original method quotation from B", 3, extraction_id=2)
    signals = {
        "a": FactSignals(datasets=(first, second), methods=(method1,)),
        "b": FactSignals(datasets=(third,), methods=(method2,)),
    }
    (match,) = _facts(signals=signals)
    (dataset,) = match.datasets
    assert dataset.normalized_name == "mnist dataset"
    assert dataset.here == (first, second) and dataset.there == (third,)
    (method,) = match.methods
    assert method.normalized_name == "attention network"
    assert method.here == (method1,) and method.there == (method2,)


def test_related_acronyms_or_semantic_names_are_not_guessed_equal():
    signals = {
        "a": FactSignals(
            datasets=(_field("MNIST", "Dataset used in original A", 0),),
            methods=(_field("CNN", "Method used in original A", 1),),
        ),
        "b": FactSignals(
            datasets=(_field("MNIST handwritten digits", "Dataset used in original B", 0),),
            methods=(_field("convolutional neural network", "Method used in original B", 1),),
        ),
    }
    assert not _facts(signals=signals)
