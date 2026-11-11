"""综述骨架真实队列：已读范围、冻结附件、证据资格与历史缓存。"""

import json
from dataclasses import replace

import pytest

from litgleaner.application import outline
from litgleaner.application.llm_gateway import Prompt
from litgleaner.application.pipeline import has_on_demand_work, run_on_demand
from litgleaner.application.topics import pause_topic
from litgleaner.domain.errors import InvalidRequest, ModelConfigurationRequired, TransientError
from litgleaner.domain.outline import TopicOutlineSettings
from litgleaner.domain.outline_render import render_outline
from tests.e2e.conftest import Harness
from tests.e2e.outline_helpers import TOPIC, add_read_paper, generate, setup_outline


def test_readonly_preview_then_confirmed_input_generates_once_with_source_attachments(harness):
    ctx, ids = setup_outline(harness)
    add_read_paper(harness, "Unread alpha", read=False)
    add_read_paper(harness, "Unrelated alpha", relevant=False)
    planned = outline.preview(ctx, TOPIC)
    assert planned.total_papers == 3 and planned.input_paper_ids == ids
    assert planned.estimated_usd is None and planned.cached_outline_id is None
    assert not has_on_demand_work(ctx) and harness.llm_calls() == 0
    assert outline.view(ctx, TOPIC).result is None
    queued = outline.queue(ctx, TOPIC, planned.plan_hash)
    assert queued.status == "pending" and has_on_demand_work(ctx)
    assert outline.queue(ctx, TOPIC, planned.plan_hash).job_key == queued.job_key
    assert run_on_demand(ctx).ok
    done = outline.status(ctx, TOPIC, queued.job_key)
    assert done.status == "done" and done.result.input_paper_ids == ids
    assert done.cost_usd == pytest.approx(0.001) and harness.llm_calls("outline") == 1
    assert all(paper.bibtex and json.loads(paper.csl_json) for paper in done.result.papers)
    assert "Alpha method" in render_outline(done.result)
    assert "10.1000/alpha-read-0" in render_outline(done.result)
    assert "Unread alpha" not in harness.llm.calls[-1].user
    assert "Unrelated alpha" not in render_outline(done.result)
    cached = outline.preview(ctx, TOPIC)
    assert cached.estimated_usd == 0 and cached.cached_outline_id == done.result.id
    assert outline.queue(ctx, TOPIC, planned.plan_hash).result == done.result
    assert run_on_demand(ctx).ok and harness.llm_calls("outline") == 1
    assert harness.publisher().sent == []


def test_unknown_ids_invalid_goals_and_incomplete_gap_basis_are_cleaned(harness):
    ctx, ids = setup_outline(harness)
    incomplete = add_read_paper(harness, "Incomplete alpha", evidence=False)
    harness.llm.overrides["outline"] = {
        "groups": [
            {
                "name": "a",
                "description": "short",
                "goal_ids": ["g1", "invented"],
                "paper_ids": [ids[0], "invented", ids[0]],
            },
            {"name": "b", "description": "short", "goal_ids": [], "paper_ids": [ids[0], ids[1]]},
            {"name": " ", "description": "short", "goal_ids": [], "paper_ids": [ids[2]]},
        ],
        "gaps": [
            {"description": "有证据的当前集合候选", "paper_ids": [ids[0], ids[1]]},
            {"description": "伪造编号", "paper_ids": [ids[0], "invented"]},
            {"description": "错误地把未提取当成未研究", "paper_ids": [ids[0], incomplete]},
            {"description": "无依据", "paper_ids": []},
        ],
    }
    done = generate(ctx)
    assert [group.name for group in done.result.groups] == ["a", "b"]
    assert done.result.groups[0].goal_ids == ("g1",)
    assert done.result.groups[1].paper_ids == (ids[1],)
    assert [gap.description for gap in done.result.gaps] == ["有证据的当前集合候选"]
    assert set(done.result.ungrouped) == {ids[2], incomplete}
    assert done.result.warnings and harness.llm_calls("outline") == 1
    assert "仅限本次已读输入集合" in render_outline(done.result)


def test_model_can_abstain_without_schema_retries_or_invented_groups(harness):
    ctx, _ids = setup_outline(harness, count=0)
    id_ = add_read_paper(harness, "No extracted evidence", evidence=False)
    harness.llm.overrides["outline"] = {"groups": [], "gaps": []}
    done = generate(ctx)
    assert done.status == "done" and done.result.groups == () and done.result.gaps == ()
    assert done.result.ungrouped == (id_,) and harness.llm_calls("outline") == 1


def change_context(ctx, kind, id_):
    if kind == "topic":
        return replace(
            ctx,
            topics={TOPIC: ctx.topics[TOPIC].model_copy(update={"background": "Changed profile"})},
        )
    if kind == "prompt":
        route = ctx.llm.routes["outline"]
        return replace(
            ctx,
            llm=replace(
                ctx.llm,
                routes={
                    **ctx.llm.routes,
                    "outline": replace(
                        route, prompt=Prompt("outline", route.prompt.text + " changed")
                    ),
                },
            ),
        )
    if kind == "language":
        return replace(ctx, settings=replace(ctx.settings, reading_language="en"))
    if kind == "budget":
        return replace(
            ctx, settings=replace(ctx.settings, outline=TopicOutlineSettings(token_budget=16000))
        )
    if kind == "reading":
        ctx.store.reader.set_state(id_, "reading", {}, ctx.now_iso())
    else:
        ctx.store.papers.insert_extraction(
            id_,
            "new-input",
            "new-producer",
            {"method": {"text": "Changed method", "verified": False}},
            ctx.now_iso(),
        )
    return ctx


@pytest.mark.parametrize("change", ["topic", "prompt", "language", "budget", "reading", "source"])
def test_changed_preview_requires_confirmation_again_without_model_call(harness, change):
    ctx, ids = setup_outline(harness)
    planned = outline.preview(ctx, TOPIC)
    changed = change_context(ctx, change, ids[0])
    with pytest.raises(InvalidRequest, match="重新预览"):
        outline.queue(changed, TOPIC, planned.plan_hash)
    assert harness.llm_calls("outline") == 0


@pytest.mark.parametrize("change", ["topic", "prompt", "language", "budget", "reading", "source"])
def test_changed_queued_sources_or_configuration_die_before_model_call(harness, change):
    ctx, ids = setup_outline(harness)
    queued = outline.queue(ctx, TOPIC, outline.preview(ctx, TOPIC).plan_hash)
    changed = change_context(ctx, change, ids[0])
    assert not run_on_demand(changed).ok
    status = outline.status(changed, TOPIC, queued.job_key)
    assert status.status == "dead" and status.result is None
    assert "重新预览" in status.last_error and status.cost_usd == 0
    assert harness.llm_calls("outline") == 0


def test_later_read_paper_is_only_reported_and_never_expands_confirmed_input(harness):
    ctx, ids = setup_outline(harness)
    queued = outline.queue(ctx, TOPIC, outline.preview(ctx, TOPIC).plan_hash)
    added = add_read_paper(harness, "New read alpha")
    assert run_on_demand(ctx).ok
    saved = outline.status(ctx, TOPIC, queued.job_key).result
    assert saved.input_paper_ids == ids and "New read alpha" not in harness.llm.calls[-1].user
    view = outline.view(ctx, TOPIC)
    assert view.new_paper_ids == (added,) and view.ungrouped == (added,)
    assert not view.stale and view.total_papers == 4 and view.result == saved
    assert not has_on_demand_work(ctx) and harness.llm_calls("outline") == 1


def test_historical_export_retains_frozen_facts_after_source_changes(harness):
    ctx, ids = setup_outline(harness)
    first = generate(ctx)
    text = render_outline(first.result)
    change_context(ctx, "source", ids[0])
    assert outline.view(ctx, TOPIC).stale
    assert render_outline(outline.result(ctx, TOPIC, first.result.id)) == text
    second = generate(ctx)
    assert second.result.id != first.result.id and harness.llm_calls("outline") == 2
    assert not outline.view(ctx, TOPIC).stale
    assert render_outline(second.result) != text


@pytest.mark.parametrize("change", ["language", "prompt", "budget"])
def test_switching_back_reuses_correct_historical_cache_without_fees(harness, change):
    ctx, ids = setup_outline(harness)
    first = generate(ctx)
    changed = change_context(ctx, change, ids[0])
    second = generate(changed)
    assert first.result.id != second.result.id
    planned = outline.preview(ctx, TOPIC)
    assert planned.cached_outline_id == first.result.id and planned.estimated_usd == 0
    assert outline.queue(ctx, TOPIC, planned.plan_hash).result == first.result
    assert outline.view(ctx, TOPIC).result == first.result and not outline.view(ctx, TOPIC).stale
    assert harness.llm_calls("outline") == 2


def test_budget_deferred_and_transient_retry_keep_scope_and_no_background_regeneration(harness):
    ctx, _ids = setup_outline(harness)
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=0))
    queued = outline.queue(ctx, TOPIC, outline.preview(ctx, TOPIC).plan_hash)
    assert run_on_demand(ctx).budget_stopped and harness.llm_calls("outline") == 0
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=1))
    harness.llm.fail_tasks["outline"] = [TransientError("offline transient")]
    assert not run_on_demand(ctx).ok
    harness.clock.advance(minutes=6)
    assert run_on_demand(ctx).ok
    status = outline.status(ctx, TOPIC, queued.job_key)
    assert status.status == "done" and status.cost_usd == pytest.approx(0.001)
    assert run_on_demand(ctx).ok and len(ctx.store.topic_outlines.history(TOPIC)) == 1
    assert harness.llm_calls("outline") == 1


def test_budget_omitted_ids_are_never_eligible_for_group_or_gap(harness):
    ctx, ids = setup_outline(harness, count=30)
    ctx = replace(
        ctx, settings=replace(ctx.settings, outline=TopicOutlineSettings(token_budget=4096))
    )
    planned = outline.preview(ctx, TOPIC)
    assert 0 < len(planned.input_paper_ids) < len(ids) and planned.input_tokens <= 4096
    omitted = planned.omitted_paper_ids[0]
    harness.llm.overrides["outline"] = {
        "groups": [{"name": "bad", "description": "bad", "goal_ids": [], "paper_ids": [omitted]}],
        "gaps": [{"description": "bad", "paper_ids": [planned.input_paper_ids[0], omitted]}],
    }
    done = generate(ctx)
    assert done.result.groups == () and done.result.gaps == ()
    assert set(done.result.ungrouped) == set(ids)
    request = json.loads(harness.llm.calls[-1].user.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert omitted not in {paper["paper_id"] for paper in request["papers"]}


def test_empty_profile_too_large_and_missing_route_do_not_create_paid_work(harness):
    ctx, _ids = setup_outline(harness, count=0)
    planned = outline.preview(ctx, TOPIC)
    assert planned.total_papers == 0 and planned.estimated_usd == 0
    with pytest.raises(InvalidRequest, match="已读论文"):
        outline.queue(ctx, TOPIC, planned.plan_hash)
    topic = ctx.topics[TOPIC].model_copy(update={"background": "过长画像" * 20000})
    with pytest.raises(InvalidRequest, match="输入预算"):
        outline.preview(replace(ctx, topics={TOPIC: topic}), TOPIC)
    missing = replace(ctx, llm=replace(ctx.llm, routes={}))
    assert outline.preview(missing, TOPIC).producer_version is None
    with pytest.raises(ModelConfigurationRequired):
        outline.queue(missing, TOPIC, planned.plan_hash)
    assert harness.llm_calls("outline") == 0 and not has_on_demand_work(ctx)


def test_paused_topic_and_missing_models_can_read_saved_results(harness: Harness):
    ctx, _ids = setup_outline(harness)
    pause_topic(ctx, TOPIC)
    done = generate(ctx)
    offline = replace(ctx, llm=replace(ctx.llm, routes={}))
    assert outline.view(offline, TOPIC).result == done.result
    assert outline.result(offline, TOPIC, done.result.id) == done.result
    assert harness.llm_calls("outline") == 1
