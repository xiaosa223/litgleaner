"""阅读记忆投影保留用户原文、关系方向及每个来源的独立证据。"""

from dataclasses import FrozenInstanceError, asdict, replace

import pytest

from litgleaner.domain.citation import Citation
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.identity import Alias
from litgleaner.domain.memory import MemoryPaper
from litgleaner.domain.memory_citations import citation_index
from litgleaner.domain.paper_relation import PaperRelation, RelationEvidence
from litgleaner.domain.reading import Annotation
from litgleaner.domain.reading_memory import (
    ReadingMemory,
    citation_links,
    note_excerpts,
    relation_cards,
)

NOW = "2026-11-12T00:00:00+00:00"


def _note(id_="note", paper_id="B", body="My original conclusion.", **changes):
    note = Annotation(id_, paper_id, "note", None, None, "Author original quote.", body, NOW, NOW)
    return replace(note, **changes)


def _papers(*ids):
    return {
        id_: MemoryPaper(id_, f"Paper {id_}", "read" if id_ == "B" else "reading") for id_ in ids
    }


def _citation(citing="A", cited="B", *, position="0", source="parsed"):
    return Citation(
        citing,
        position,
        f"Original reference {position}",
        source,
        2,
        cited,
        f"Paper {cited}",
        (),
        (f"Original context {position}.",),
        ("methodology",),
        True,
        NOW,
        3,
        (1, 2, 3, 4),
    )


def _relation(a="A", b="B", kind="extends", id_="relation"):
    return PaperRelation(
        id=id_,
        batch_id="batch",
        paper_a=a,
        paper_b=b,
        kind=kind,
        evidence_a=RelationEvidence(
            paper_id=a, doc_key=f"doc-{a}", page=1, quote=f"Original evidence for paper {a}."
        ),
        evidence_b=RelationEvidence(
            paper_id=b, doc_key=f"doc-{b}", page=2, quote=f"Original evidence for paper {b}."
        ),
        explanation="Frozen explanation.",
        source_hashes={a: f"hash-{a}", b: f"hash-{b}"},
        input_hash="input",
        producer_version="producer",
        created_at=NOW,
    )


def test_note_keeps_user_body_identity_version_and_original_missing_page():
    note = _note(quote="A different author quote.")
    excerpt = note_excerpts((note,))["B"]
    assert excerpt.text == note.body and excerpt.text != note.quote
    assert (excerpt.source_id, excerpt.paper_id, excerpt.page) == (note.id, "B", None)
    assert excerpt.authorship == "user" and not excerpt.truncated
    assert excerpt.source_version == stable_hash(asdict(note))


@pytest.mark.parametrize("body", [None, "", " \t\n\r ", "\u3000\n\u3000"])
def test_author_highlight_without_user_body_never_becomes_a_user_note(body):
    assert note_excerpts((_note(body=body, kind="highlight", page=4),)) == {}


def test_latest_nonempty_body_wins_and_same_timestamp_uses_descending_id():
    old = _note("old", body="Old user text.")
    latest = _note("latest", body="Latest user text.", updated_at="2026-11-13")
    tie = _note("z-tie", body="Latest tie breaker.", updated_at="2026-11-13")
    no_body = _note("zz-empty", body=None, updated_at="2026-11-14")
    found = note_excerpts((no_body, tie, old, latest))
    assert found["B"].source_id == "z-tie" and found["B"].text == tie.body
    assert found == note_excerpts((old, latest, tie, no_body))


def test_note_selection_is_independent_for_each_paper_and_preserves_page_zero():
    first, second = _note("first", "A", page=0), _note("second", "B", page=7)
    found = note_excerpts((first, second))
    assert set(found) == {"A", "B"}
    assert found["A"].page == 0 and found["B"].page == 7


@pytest.mark.parametrize(
    "body,text,truncated",
    [
        ("\n \n  First line.  ", "First line.", False),
        ("First line.\nSecond line.", "First line.", True),
        ("First line.\n\n", "First line.", True),
        ("x" * 240, "x" * 240, False),
        ("x" * 241, "x" * 240, True),
        ("前" * 241, "前" * 240, True),
        ("Line with  two spaces.\r\nOther.", "Line with  two spaces.", True),
    ],
)
def test_excerpt_is_one_contiguous_prefix_without_normalization_or_joining(body, text, truncated):
    excerpt = note_excerpts((_note(body=body),))["B"]
    assert excerpt.text == text and excerpt.text in body
    assert len(excerpt.text) <= 240 and excerpt.truncated is truncated


def test_user_edit_changes_source_version_even_when_visible_prefix_is_the_same():
    first = _note(body="Same first line.\nOriginal tail.")
    second = replace(first, body="Same first line.\nEdited tail.")
    left, right = note_excerpts((first,))["B"], note_excerpts((second,))["B"]
    assert left.text == right.text and left.source_version != right.source_version


def test_bidirectional_citations_group_by_sorted_peer_and_keep_every_original_object():
    papers = _papers("C", "B", "A")
    outbound = _citation()
    another_source = replace(outbound, source="remote", position="remote:9", source_position=9)
    inbound = _citation("B", "A", position="back")
    later_peer = _citation("C", "A", position="later")
    facts = (later_peer, outbound, another_source, inbound)
    notes = note_excerpts((_note(),))
    links = citation_links("A", papers, citation_index(facts, {}, {}), notes)
    assert [link.paper.paper_id for link in links] == ["B", "C"]
    assert links[0].cites == (outbound, another_source) and links[0].cited_by == (inbound,)
    assert links[0].cites[0] is outbound and links[0].cites[1] is another_source
    assert links[0].cited_by[0] is inbound and links[0].note is notes["B"]
    assert links[1].cites == () and links[1].cited_by == (later_peer,)
    assert links[1].note is None and links[0].paper.reading_status == "read"


def test_citation_projection_excludes_self_foreign_peers_and_unrelated_pairs():
    facts = (
        _citation("A", "A"),
        _citation("A", "outside"),
        _citation("outside", "A"),
        _citation("B", "C"),
    )
    index = citation_index(facts, {}, {})
    assert citation_links("A", _papers("A", "B", "C"), index, {}) == ()
    assert citation_links("A", _papers("B", "C"), index, {}) == ()


def test_cross_entry_alias_ambiguity_never_becomes_a_dynamic_direct_link():
    local, outside, x, y = tuple(Alias("doi", value) for value in ("local", "outside", "x", "y"))
    dynamic = replace(_citation(), cited_paper_id=None, cited_aliases=(local, x))
    bridge = replace(_citation("bridge"), cited_paper_id=None, cited_aliases=(x, y))
    ambiguous = replace(_citation("another"), cited_paper_id=None, cited_aliases=(y, outside))
    explicit = _citation(position="explicit-fact")
    index = citation_index(
        (dynamic, bridge, ambiguous, explicit), {"B": (local,), "outside": (outside,)}, {}
    )
    # 全局目标已排除该链，但旧direct仍含动态命中；投影保留的只能是显式事实。
    assert dynamic in index.direct["A", "B"] and not index.by_paper
    (link,) = citation_links("A", _papers("A", "B"), index, {})
    assert link.cites == (explicit,) and link.cites[0] is explicit


def test_accepted_transitive_alias_resolution_keeps_original_evidence():
    known, shared = Alias("doi", "known"), Alias("s2", "shared")
    unresolved = replace(_citation(), cited_paper_id=None, cited_aliases=(shared,))
    bridge = replace(_citation("C"), cited_paper_id=None, cited_aliases=(shared, known))
    index = citation_index((unresolved, bridge), {"B": (known,)}, {})
    assert ("A", "B") not in index.direct
    (link,) = citation_links("A", _papers("A", "B", "C"), index, {})
    assert link.paper.paper_id == "B" and link.cites == (unresolved,)
    assert link.cites[0] is unresolved


@pytest.mark.parametrize("kind", ["extends", "uses_method_of", "compares_with"])
@pytest.mark.parametrize("main,direction,other", [("A", "outgoing", "B"), ("B", "incoming", "A")])
def test_directed_relations_keep_their_frozen_orientation(kind, main, direction, other):
    relation = _relation(kind=kind)
    note = _note(paper_id=other)
    notes = note_excerpts((note,))
    (card,) = relation_cards(main, _papers("A", "B"), (relation,), notes)
    assert card.direction == direction and card.paper.paper_id == other
    assert card.relation is relation and card.note is notes[other]
    assert card.relation.evidence_a.paper_id == "A" and card.relation.evidence_b.paper_id == "B"
    assert card.relation.source_hashes == {"A": "hash-A", "B": "hash-B"}


@pytest.mark.parametrize("kind", ["contradicts", "similar_approach", "same_problem"])
@pytest.mark.parametrize("main", ["A", "B"])
def test_symmetric_relations_do_not_claim_a_forward_or_reverse_direction(kind, main):
    relation = _relation(kind=kind)
    (card,) = relation_cards(main, _papers("A", "B"), (relation,), {})
    assert card.direction == "symmetric" and card.relation is relation and card.note is None


def test_relation_cards_preserve_app_selection_order_and_skip_unrelated_or_nonlibrary_peers():
    first, second = _relation("C", "A", id_="first"), _relation("A", "B", id_="second")
    unrelated, outside = _relation("B", "C", id_="other"), _relation("A", "D", id_="outside")
    cards = relation_cards("A", _papers("A", "B", "C"), (first, unrelated, outside, second), {})
    assert tuple(card.relation for card in cards) == (first, second)
    assert relation_cards("A", _papers("B", "C"), (first, second), {}) == ()


def test_reading_memory_and_its_source_views_are_frozen():
    papers = _papers("A", "B")
    notes = note_excerpts((_note(),))
    cards = relation_cards("A", papers, (_relation(),), notes)
    links = citation_links("A", papers, citation_index((_citation(),), {}, {}), notes)
    view = ReadingMemory(papers["A"], cards, links, 1, 50, 0, 1, 1, False, ())
    for value, field, changed in (
        (view, "total", 0),
        (cards[0], "direction", "incoming"),
        (links[0], "note", None),
        (notes["B"], "text", "Different text."),
    ):
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, changed)
