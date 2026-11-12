"""候选合并真实文库信号，策略重排不产生模型费用或新判断。"""

from dataclasses import replace

import pytest
from sqlalchemy import update

from litgleaner.application import embeddings, memory_candidates
from litgleaner.domain.identity import Alias
from litgleaner.domain.memory_policy import MemoryPolicy
from litgleaner.domain.reading import NewAnnotation
from litgleaner.infra.db.schema import paper
from tests.e2e.embedding_helpers import setup
from tests.e2e.memory_helpers import add, citation, extract


def policy(harness, **changes):
    settings = replace(harness.ctx.settings, memory_policy=MemoryPolicy(**changes))
    harness.ctx = replace(harness.ctx, settings=settings)


def abstract(harness, paper_id, text):
    harness.ctx.store._db.write(update(paper).where(paper.c.id == paper_id).values(abstract=text))


def refs(harness, paper_id, *, count=2, source="parsed"):
    for index in range(count):
        citation(
            harness,
            paper_id,
            aliases=(Alias("doi", f"10.1000/ref-{index}"),),
            source=source,
            position=f"{source}:ref:{index}",
        )


def test_all_eight_signals_have_bounded_contributions_and_traceable_sources(harness):
    setup(harness)
    a, b = add(harness, "anchor"), add(harness, "neighbor")
    for key in (a, b):
        abstract(harness, key, "rarepapertoken")
        extract(harness, key)
        refs(harness, key)
    citation(harness, a, target=b, position="direct")
    embeddings.reembed(harness.ctx)
    result = memory_candidates.candidates(harness.ctx, a)
    (match,) = result.matches
    assert match.facts.paper.paper_id == b and result.total == 1
    assert match.score.total == pytest.approx(29)
    assert sum(part.contribution for part in match.score.components) == match.score.total
    assert match.signals.common_references == 2 and match.signals.core_concepts == 1
    assert match.signals.methods == match.signals.datasets == 1
    assert match.signals.cosine == match.signals.text == 1
    assert match.vector.embedder_version == "producer-v1"
    assert match.vector.here_input_hash != match.vector.there_input_hash
    assert match.text_hits and all(hit.bm25 < 0 for hit in match.text_hits)
    assert result.policy_hash == harness.ctx.settings.memory_policy.policy_hash


def test_duplicate_sources_both_directions_and_positions_do_not_raise_fact_weight(harness):
    a, b = add(harness, "anchor"), add(harness, "neighbor")
    citation(harness, a, target=b)
    first = memory_candidates.candidates(harness.ctx, a).matches[0]
    for index in range(4):
        citation(harness, a, target=b, source=f"source-{index}", position=f"direct-{index}")
    citation(harness, b, target=a)
    second = memory_candidates.candidates(harness.ctx, a).matches[0]
    assert len(second.facts.cites) == 5 and len(second.facts.cited_by) == 1
    assert first.score == second.score and second.score.total == 11


def test_common_reference_threshold_uses_distinct_targets_and_policy_changes_only_reorder(harness):
    a, b = add(harness, "anchor"), add(harness, "neighbor")
    for key in (a, b):
        refs(harness, key, count=1)
        refs(harness, key, count=1, source="another")
    assert not memory_candidates.candidates(harness.ctx, a).matches
    for key in (a, b):
        refs(harness, key)
    before = memory_candidates.candidates(harness.ctx, a)
    assert before.matches[0].score.total == 4
    policy(harness, common_reference_min=3)
    after = memory_candidates.candidates(harness.ctx, a)
    assert after.policy_hash != before.policy_hash and not after.matches
    assert len(harness.ctx.store.citations.outgoing(a)) == 3


def test_vector_only_candidates_are_not_lost_to_fact_projection_and_respect_threshold(harness):
    model = setup(harness)
    a, b, c = (add(harness, name) for name in ("anchor", "neighbor", "third"))
    model.vectors["title: neighbor"] = (0.8, 0.6, 0)
    model.vectors["title: third"] = (-1, 0, 0)
    embeddings.reembed(harness.ctx)
    first = memory_candidates.candidates(harness.ctx, a)
    assert [item.facts.paper.paper_id for item in first.matches] == [b]
    assert first.matches[0].score.total == pytest.approx(2.4)
    assert not first.matches[0].facts.cites and first.matches[0].vector is not None
    policy(harness, cosine_min=0.9)
    assert not memory_candidates.candidates(harness.ctx, a).matches
    assert c != a and len(model.calls) == 1


def test_text_only_candidates_are_normalized_after_scope_and_exclude_user_notes(harness):
    a, b, c = (add(harness, name) for name in ("anchor", "neighbor", "third"))
    for key in (a, b):
        abstract(harness, key, "rarepapertoken")
    outside = add(harness, "outside", library=False)
    abstract(harness, outside, "rarepapertoken " * 20)
    harness.ctx.store.reader.add_annotation(
        NewAnnotation(c, "note", None, None, None, "rarepapertoken"), harness.ctx.now_iso()
    )
    result = memory_candidates.candidates(harness.ctx, a)
    assert [item.facts.paper.paper_id for item in result.matches] == [b]
    (match,) = result.matches
    assert match.signals.text == match.score.total == 1
    assert match.signals.cosine is None and match.vector is None
    assert all(hit.kind == "paper" for hit in match.text_hits)


def test_all_library_candidates_are_ranked_before_policy_and_request_limits(harness):
    a = add(harness, "anchor")
    others = [add(harness, f"neighbor-{index:03}") for index in range(205)]
    for other in others:
        citation(harness, a, target=other, position=other)
    result = memory_candidates.candidates(harness.ctx, a)
    assert result.total == 205 and result.limit == 5
    assert [item.facts.paper.paper_id for item in result.matches] == sorted(others)[:5]
    assert len(memory_candidates.candidates(harness.ctx, a, limit=2).matches) == 2
    assert len(memory_candidates.candidates(harness.ctx, a, limit=200).matches) == 5
    policy(harness, max_pairs=200)
    assert len(memory_candidates.candidates(harness.ctx, a).matches) == 200


@pytest.mark.parametrize(
    "contexts,intents,expected",
    [
        ((), ("methodology",), 6),
        (("A genuine context.",), ("background",), 6),
        (("A genuine context.",), ("Methodology",), 11),
        (("   ",), ("extends",), 6),
        (("A genuine context.",), ("extension",), 11),
    ],
)
def test_intent_bonus_requires_both_a_context_and_an_explicit_priority_intent(
    harness, contexts, intents, expected
):
    a, b = add(harness, "anchor"), add(harness, "neighbor")
    citation(harness, a, target=b, contexts=contexts, intents=intents)
    assert memory_candidates.candidates(harness.ctx, a).matches[0].score.total == expected


def test_zero_weight_policy_never_returns_zero_score_pairs_even_with_zero_threshold(harness):
    a, b = add(harness, "anchor"), add(harness, "neighbor")
    citation(harness, a, target=b)
    weights = dict.fromkeys(MemoryPolicy().weights.model_dump(), 0)
    policy(harness, weights=weights, minimum_score=0)
    assert memory_candidates.candidates(harness.ctx, a).total == 0
