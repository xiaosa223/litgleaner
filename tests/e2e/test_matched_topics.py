"""不可变推送保存全部有效命中课题；公开发送使用既有字段白名单。"""

from dataclasses import replace

import pytest
from sqlalchemy import select

from litgleaner.application.cards import build_card, card_from_dict
from litgleaner.application.stages.publish import rerender
from litgleaner.application.topics import activate_topic, pause_topic
from litgleaner.infra.db.schema import digest
from litgleaner.ports.store import DigestItem, TriageResult
from tests.e2e.conftest import paper
from tests.e2e.test_change_scenarios import add_topic


@pytest.fixture
def shared(harness):
    add_topic(harness, "beta-topic", name="Private Beta")
    harness.edit_yaml("topics/alpha-topic.yaml", lambda data: data.update(public=True))
    harness.rebuild()
    activate_topic(harness.ctx, "beta-topic")
    harness.clock.before_push()
    harness.clock.advance(hours=1)
    harness.write_papers(
        [paper("Shared Hit", 5, abstract="Research on alpha and beta-topic. [[level:5]]")]
    )
    assert harness.run().ok
    rows = harness.ctx.store._db.fetch(select(digest.c.id).where(digest.c.channel == "me"))
    assert len(rows) == 1
    record = harness.ctx.store.outputs.get_digest(rows[0].id)
    assert record is not None
    data = harness.ctx.store.outputs.digest_cards(record.id)[0]
    return harness, record, data


def names(data):
    return [(topic.topic_id, topic.name) for topic in card_from_dict(data).matched_topics]


def fresh(ctx, data):
    scored = ctx.store.judgments.scored_for_paper(data["paper_id"])[0]
    topic = ctx.topics[scored.assessment.topic_id]
    return build_card(ctx, scored, ctx.rubrics[topic.rubric])


def test_shared_paper_is_sent_once_with_all_topics_and_no_extra_model_calls(shared):
    harness, _record, data = shared
    assert harness.pushed_titles() == ["Shared Hit"]
    assert names(data) == [("alpha-topic", "Alpha"), ("beta-topic", "Private Beta")]
    assert "命中课题：Alpha、Private Beta" in harness.publisher().sent[0].body
    assert harness.llm_calls("triage") == harness.llm_calls("assess") == 2
    assert harness.llm_calls("extract") == 1
    before = harness.llm_calls()
    assert names(fresh(harness.ctx, data)) == names(data)
    assert harness.llm_calls() == before


def test_actual_public_publish_never_discloses_the_private_topic_link(shared):
    harness, _record, _data = shared
    public = harness.publisher("web").sent
    assert len(public) == 1
    assert "Private Beta" not in public[0].body
    assert "beta-topic" not in public[0].body
    assert "命中课题" not in public[0].body


@pytest.mark.parametrize("change", ["stale", "deleted"])
def test_new_card_omits_old_profile_or_removed_topic(shared, change):
    harness, _record, data = shared
    topics = dict(harness.ctx.topics)
    if change == "stale":
        topics["beta-topic"] = topics["beta-topic"].model_copy(update={"background": "changed"})
    else:
        topics.pop("beta-topic")
    ctx = replace(harness.ctx, topics=topics)
    before = harness.llm_calls()
    assert names(fresh(ctx, data)) == [("alpha-topic", "Alpha")]
    assert harness.llm_calls() == before


def test_only_triaged_topic_is_a_valid_match_without_an_assessment(shared):
    harness, _record, data = shared
    gamma = harness.ctx.topics["alpha-topic"].model_copy(
        update={"id": "gamma-topic", "name": "Gamma", "background": "gamma background"}
    )
    ctx = replace(harness.ctx, topics={**harness.ctx.topics, gamma.id: gamma})
    ctx.store.judgments.save_triage(
        TriageResult(data["paper_id"], gamma.id, gamma.topic_hash, True, "Relevant", "v", "rule"),
        ctx.now_iso(),
    )
    before = harness.llm_calls()
    assert names(fresh(ctx, data))[-1] == (gamma.id, gamma.name)
    assert ctx.store.judgments.assessment_keys(data["paper_id"], gamma.id) == ()
    assert harness.llm_calls() == before


def test_triage_order_duplicates_and_obsolete_fingerprints_do_not_change_topics(
    shared, monkeypatch
):
    harness, _record, data = shared
    original = harness.ctx.store.judgments.relevant_topics(data["paper_id"])
    changed = (*reversed(original), original[0], ("beta-topic", "old-hash"), ("removed", "hash"))
    monkeypatch.setattr(harness.ctx.store.judgments, "relevant_topics", lambda _: changed)
    assert names(fresh(harness.ctx, data)) == names(data)


def test_pausing_keeps_existing_current_matches_without_recomputing(shared):
    harness, _record, data = shared
    before = harness.llm_calls()
    pause_topic(harness.ctx, "beta-topic")
    assert names(fresh(harness.ctx, data)) == names(data)
    assert harness.llm_calls() == before


def test_rerender_uses_frozen_names_after_rename_and_removal(shared):
    harness, record, data = shared
    alpha = harness.ctx.topics["alpha-topic"].model_copy(update={"name": "Renamed Alpha"})
    ctx = replace(harness.ctx, topics={alpha.id: alpha})
    before = harness.llm_calls()
    rendered = rerender(ctx, record.id, "me")
    assert rendered is not None
    assert "命中课题：Alpha、Private Beta" in rendered.body
    assert harness.ctx.store.outputs.digest_cards(record.id)[0] == data
    assert names(fresh(ctx, data)) == [("alpha-topic", "Renamed Alpha")]
    assert harness.llm_calls() == before


def test_persisted_legacy_digest_without_topic_field_still_rerenders(shared):
    harness, record, data = shared
    legacy = {key: value for key, value in data.items() if key != "matched_topics"}
    harness.ctx.store.outputs.create_digest(
        replace(record, id="legacy", route_id="legacy-route"),
        (DigestItem(data["paper_id"], False, legacy),),
        harness.ctx.now_iso(),
    )
    rendered = rerender(harness.ctx, "legacy", "me")
    assert rendered is not None and "Shared Hit" in rendered.body
    assert "命中课题" not in rendered.body
    assert card_from_dict(legacy).matched_topics == ()
