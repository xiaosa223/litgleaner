"""推荐理由随 assessment 保存；不修改事实、判重键或阅读价值分。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from litgleaner.domain.goal_links import GoalLink
from litgleaner.domain.paper import Paper
from litgleaner.domain.rubric import Rubric, compute_score
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import NewAssessment, RankQuery


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'goal-links.db'}"
    upgrade_database(url)
    result = SqlStore(url)
    for paper_id in ("P1", "P2"):
        result.papers.insert(
            Paper(paper_id, "Original", "original", None, (), None, None, None, "t")
        )
    yield result
    result.dispose()


def _assessment(paper_id="P1", **changes):
    return replace(
        NewAssessment(
            paper_id,
            "topic",
            "topic-hash",
            "semantic",
            "input",
            {"value": 4},
            {"value": {"reason": "Original evidence"}},
            True,
            "model",
            "v1",
        ),
        **changes,
    )


def _links():
    return (
        GoalLink(
            goal_id="g1",
            reuse="借鉴原文方法",
            quote="An original quotation.",
            page=0,
            verified=True,
        ),
        GoalLink(
            goal_id="g2", reuse="尚无原文定位的推荐理由", quote=None, page=None, verified=False
        ),
    )


def test_links_roundtrip_with_exact_typed_quote_page_verified_and_original_evidence(store):
    item = _assessment(goal_links=_links())
    store.judgments.insert_assessment(item, "t")
    found = store.judgments.get_assessment(1)
    assert found.goal_links == item.goal_links and all(
        isinstance(link, GoalLink) for link in found.goal_links
    )
    assert found.levels == item.levels and found.evidence == item.evidence
    assert found.goal_links[0].page == 0 and found.goal_links[0].verified is True
    assert found.goal_links[1].quote is found.goal_links[1].page is None
    assert found.goal_links[1].verified is False
    assert store.judgments.get_assessment(999) is None


def test_duplicate_assessment_keeps_original_goal_links_and_does_not_create_new_key(store):
    item = _assessment(goal_links=_links())
    store.judgments.insert_assessment(item, "t")
    before = store.judgments.get_assessment(1)
    keys = store.judgments.assessment_keys("P1", "topic")
    store.judgments.insert_assessment(replace(item, goal_links=(), evidence={}), "t2")
    assert store.judgments.get_assessment(1) == before
    assert store.judgments.assessment_keys("P1", "topic") == keys
    assert store.judgments.get_assessment(2) is None


def test_default_empty_links_roundtrip_and_database_default_supports_legacy_inserts(store):
    store.judgments.insert_assessment(_assessment(), "t")
    assert store.judgments.get_assessment(1).goal_links == ()
    store._db.write(
        text(
            "INSERT INTO assessment(paper_id,topic_id,topic_hash,semantic_hash,input_hash,"
            "levels,evidence,fulltext_used,model,prompt_version,created_at) "
            "VALUES ('P2','topic','topic-hash','semantic','input','{}','{}',0,'m','v','t')"
        )
    )
    assert store.judgments.get_assessment(2).goal_links == ()


def test_links_are_carried_through_ranking_without_changing_score_or_reassessment_keys(store):
    path = Path(__file__).parents[2] / "config/rubrics/default.json"
    rubric = Rubric.model_validate(json.loads(path.read_text()))
    levels = {dimension.key: rubric.levels.max for dimension in rubric.dimensions}
    for paper_id, links in (("P1", ()), ("P2", _links())):
        item = _assessment(
            paper_id, levels=levels, semantic_hash=rubric.semantic_hash, goal_links=links
        )
        store.judgments.insert_assessment(item, "t")
    for assessment_id in (1, 2):
        item = store.judgments.get_assessment(assessment_id)
        score = compute_score(rubric, item.levels, fulltext_used=item.fulltext_used)
        store.judgments.save_score(assessment_id, rubric.policy_hash, score, "t")
    query = RankQuery(
        "topic", "topic-hash", rubric.semantic_hash, rubric.policy_hash, (score.tier,), "route", 50
    )
    first, second = store.judgments.rank_candidates(query)
    assert first.score == second.score and first.tier == second.tier
    assert first.assessment.goal_links == () and second.assessment.goal_links == _links()
    assert store.judgments.scored_for_paper("P2") == (second,)
    assert (
        store.judgments.unscored_assessments("topic", rubric.semantic_hash, rubric.policy_hash)
        == ()
    )
    assert store.judgments.assessment_keys("P1", "topic") == store.judgments.assessment_keys(
        "P2", "topic"
    )
