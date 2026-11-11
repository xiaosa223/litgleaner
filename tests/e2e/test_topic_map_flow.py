"""显式课题地图的真实队列、缓存、冻结输入、预算和新增论文投影。"""

import json
from dataclasses import replace

import pytest

from litgleaner.application import topic_map
from litgleaner.application.llm_gateway import Prompt
from litgleaner.application.pipeline import has_on_demand_work, run_on_demand, run_pipeline
from litgleaner.domain.errors import InvalidRequest, TransientError
from litgleaner.domain.topic_map import TopicMapSettings
from tests.e2e.conftest import Harness
from tests.e2e.topic_map_helpers import TOPIC, add_paper, setup_map


def generate(ctx):
    planned = topic_map.preview(ctx, TOPIC)
    queued = topic_map.queue(ctx, TOPIC, planned.plan_hash)
    assert run_on_demand(ctx).ok
    return topic_map.status(ctx, TOPIC, queued.job_key)


def test_preview_is_readonly_then_explicit_queue_generates_once(harness: Harness):
    ctx, ids = setup_map(harness)
    plan = topic_map.preview(ctx, TOPIC)
    assert plan.total_papers == len(ids) and set(plan.input_paper_ids) == set(ids)
    assert plan.estimated_usd is None and plan.cached_map_id is None
    assert harness.llm_calls() == 0 and not has_on_demand_work(ctx)
    assert topic_map.view(ctx, TOPIC).result is None
    queued = topic_map.queue(ctx, TOPIC, plan.plan_hash)
    assert has_on_demand_work(ctx) and queued.status == "pending"
    assert topic_map.queue(ctx, TOPIC, plan.plan_hash).job_key == queued.job_key
    assert run_on_demand(ctx).ok
    done = topic_map.status(ctx, TOPIC, queued.job_key)
    assert done.status == "done" and done.result is not None
    assert len(done.result.groups) == 5 and done.result.ungrouped == ()
    assert done.cost_usd == pytest.approx(0.001) and harness.llm_calls("map") == 1
    assert topic_map.preview(ctx, TOPIC).estimated_usd == 0
    assert topic_map.preview(ctx, TOPIC).cached_map_id == done.result.id
    assert topic_map.queue(ctx, TOPIC, plan.plan_hash).result == done.result
    assert run_on_demand(ctx).ok and harness.llm_calls("map") == 1
    request = harness.llm.calls[-1]
    assert request.task == "map" and request.schema["properties"]["groups"]["minItems"] == 5
    assert request.schema["properties"]["groups"]["maxItems"] == 8
    assert all(id_ in request.user for id_ in ids)
    assert harness.publisher().sent == []


def test_unknown_ids_duplicate_members_and_blank_groups_are_sanitized(harness: Harness):
    ctx, ids = setup_map(harness)
    groups = [
        {"name": "a", "description": "a", "paper_ids": [ids[0], "invented", ids[0]]},
        {"name": "b", "description": "b", "paper_ids": [ids[0], ids[1]]},
        {"name": "c", "description": "c", "paper_ids": ["invented"]},
        {"name": " ", "description": "d", "paper_ids": [ids[2]]},
        {"name": "e", "description": "e", "paper_ids": [ids[2]]},
    ]
    harness.llm.overrides["map"] = {"groups": groups}
    done = generate(ctx)
    assert [g.name for g in done.result.groups] == ["a", "b", "e"]
    assert tuple(id_ for group in done.result.groups for id_ in group.paper_ids) == ids[:3]
    assert set(done.result.ungrouped) == set(ids[3:])
    assert len(done.result.warnings) == 3


def test_all_invalid_assignments_remain_ungrouped_without_extra_model_calls(harness: Harness):
    ctx, ids = setup_map(harness)
    harness.llm.overrides["map"] = {
        "groups": [
            {"name": str(n), "description": "no valid paper", "paper_ids": ["invented"]}
            for n in range(5)
        ]
    }
    done = generate(ctx)
    assert not done.result.groups and set(done.result.ungrouped) == set(ids)
    assert harness.llm_calls("map") == 1


def test_new_paper_only_changes_new_count_and_is_not_automatically_mapped(harness: Harness):
    ctx, ids = setup_map(harness)
    done = generate(ctx)
    new_id = add_paper(harness, "New alpha paper")
    view = topic_map.view(ctx, TOPIC)
    assert view.new_paper_ids == (new_id,) and view.ungrouped == (new_id,)
    assert view.total_papers == len(ids) + 1 and not view.stale
    assert view.result == done.result
    assert not has_on_demand_work(ctx)
    assert run_on_demand(ctx).ok and harness.llm_calls("map") == 1


def test_new_paper_after_confirmation_does_not_expand_frozen_paid_input(harness: Harness):
    ctx, ids = setup_map(harness)
    queued = topic_map.queue(ctx, TOPIC, topic_map.preview(ctx, TOPIC).plan_hash)
    new_id = add_paper(harness, "After confirmation alpha")
    assert run_on_demand(ctx).ok
    done = topic_map.status(ctx, TOPIC, queued.job_key)
    assert set(done.result.input_paper_ids) == set(ids) and new_id not in harness.llm.calls[-1].user
    assert topic_map.view(ctx, TOPIC).new_paper_ids == (new_id,)


@pytest.mark.parametrize("change", ["topic", "prompt", "language", "budget", "source"])
def test_preview_configuration_change_requires_new_confirmation(harness: Harness, change):
    ctx, ids = setup_map(harness)
    plan = topic_map.preview(ctx, TOPIC)
    changed = change_context(ctx, change, ids[0])
    with pytest.raises(InvalidRequest, match="重新预览"):
        topic_map.queue(changed, TOPIC, plan.plan_hash)
    assert harness.llm_calls("map") == 0


@pytest.mark.parametrize("change", ["topic", "prompt", "language", "budget", "source"])
def test_queued_configuration_or_input_change_dies_without_model_call(harness: Harness, change):
    ctx, ids = setup_map(harness)
    queued = topic_map.queue(ctx, TOPIC, topic_map.preview(ctx, TOPIC).plan_hash)
    changed = change_context(ctx, change, ids[0])
    report = run_on_demand(changed)
    assert not report.ok
    current = topic_map.status(changed, TOPIC, queued.job_key)
    assert current.status == "dead" and current.result is None and current.cost_usd == 0
    assert "重新预览" in current.last_error and harness.llm_calls("map") == 0


def change_context(ctx, kind, id_):
    if kind == "topic":
        return replace(
            ctx, topics={TOPIC: ctx.topics[TOPIC].model_copy(update={"background": "new"})}
        )
    if kind == "prompt":
        route = ctx.llm.routes["map"]
        routes = {
            **ctx.llm.routes,
            "map": replace(route, prompt=Prompt("map", route.prompt.text + "new")),
        }
        return replace(ctx, llm=replace(ctx.llm, routes=routes))
    if kind == "language":
        return replace(ctx, settings=replace(ctx.settings, reading_language="en"))
    if kind == "budget":
        return replace(
            ctx, settings=replace(ctx.settings, map=TopicMapSettings(token_budget=16000))
        )
    ctx.store.papers.insert_extraction(
        id_, "fresh-input", "extract-v2", {"summary": "fresh alpha"}, ctx.now_iso()
    )
    return ctx


def test_existing_extraction_is_sent_and_later_changes_only_mark_stale(harness: Harness):
    ctx, ids = setup_map(harness)
    change_context(ctx, "source", ids[0])
    done = generate(ctx)
    assert "fresh alpha" in harness.llm.calls[-1].user
    change_context(ctx, "source", ids[1])
    view = topic_map.view(ctx, TOPIC)
    assert view.stale and "提取" in "".join(view.stale_reasons)
    assert view.result == done.result and harness.llm_calls("map") == 1


def test_budget_pause_and_transient_retry_keep_one_result_and_scope(harness: Harness):
    ctx, _ = setup_map(harness)
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=0))
    queued = topic_map.queue(ctx, TOPIC, topic_map.preview(ctx, TOPIC).plan_hash)
    assert run_on_demand(ctx).budget_stopped and harness.llm_calls("map") == 0
    ctx = replace(ctx, llm=replace(ctx.llm, daily_budget_usd=1))
    harness.llm.fail_tasks["map"] = [TransientError("offline transient")]
    assert not run_on_demand(ctx).ok
    harness.clock.advance(minutes=6)
    assert run_on_demand(ctx).ok
    done = topic_map.status(ctx, TOPIC, queued.job_key)
    assert done.status == "done" and done.cost_usd == pytest.approx(0.001)
    assert len(ctx.store.topic_maps.history(TOPIC)) == 1


def test_mixed_daily_run_does_not_regenerate_map(harness: Harness):
    ctx, _ = setup_map(harness)
    queued = topic_map.queue(ctx, TOPIC, topic_map.preview(ctx, TOPIC).plan_hash)
    assert run_pipeline(ctx).ok
    assert topic_map.status(ctx, TOPIC, queued.job_key).cost_usd == pytest.approx(0.001)
    assert run_pipeline(ctx).ok and harness.llm_calls("map") == 1


def test_omitted_paper_ids_cannot_be_assigned_and_actual_input_fits_budget(harness: Harness):
    ctx, ids = setup_map(harness, count=30)
    ctx = replace(ctx, settings=replace(ctx.settings, map=TopicMapSettings(token_budget=1024)))
    plan = topic_map.preview(ctx, TOPIC)
    assert 0 < len(plan.input_paper_ids) < len(ids)
    assert plan.omitted_paper_ids and plan.input_tokens <= 1024
    forbidden = plan.omitted_paper_ids[0]
    harness.llm.overrides["map"] = {
        "groups": [
            {"name": str(n), "description": "attempt", "paper_ids": [forbidden]} for n in range(5)
        ]
    }
    done = generate(ctx)
    assert not done.result.groups and set(done.result.ungrouped) == set(ids)
    user_data = json.loads(harness.llm.calls[-1].user.split("\n", 1)[1].rsplit("\n", 1)[0])
    assert forbidden not in {paper["paper_id"] for paper in user_data["papers"]}


def test_changed_extraction_version_can_be_explicitly_regenerated_even_if_text_projection_is_same(
    harness: Harness,
):
    ctx, ids = setup_map(harness)
    ctx.store.papers.insert_extraction(
        ids[0],
        "source-v1",
        "extract-v1",
        {"summary": "same summary", "unused": "old"},
        ctx.now_iso(),
    )
    first = generate(ctx)
    old_text = harness.llm.calls[-1].user
    ctx.store.papers.insert_extraction(
        ids[0],
        "source-v2",
        "extract-v2",
        {"summary": "same summary", "unused": "new"},
        ctx.now_iso(),
    )
    assert topic_map.view(ctx, TOPIC).stale
    plan = topic_map.preview(ctx, TOPIC)
    assert plan.cached_map_id is None and plan.input_hash != first.result.input_hash
    second = generate(ctx)
    assert not topic_map.view(ctx, TOPIC).stale
    assert first.result.id != second.result.id and harness.llm_calls("map") == 2
    assert old_text == harness.llm.calls[-1].user
    assert len(ctx.store.topic_maps.history(TOPIC)) == 2


def test_language_is_in_input_hash_even_when_custom_prompt_has_no_language_placeholder(
    harness: Harness,
):
    ctx, _ = setup_map(harness)
    route = replace(ctx.llm.routes["map"], prompt=Prompt("map", "Group the supplied papers."))
    ctx = replace(ctx, llm=replace(ctx.llm, routes={**ctx.llm.routes, "map": route}))
    first = generate(ctx)
    ctx = replace(ctx, settings=replace(ctx.settings, reading_language="en"))
    assert topic_map.view(ctx, TOPIC).stale
    assert topic_map.preview(ctx, TOPIC).cached_map_id is None
    second = generate(ctx)
    assert (
        second.result.reading_language == "en"
        and second.result.input_hash != first.result.input_hash
    )
    assert not topic_map.view(ctx, TOPIC).stale and harness.llm_calls("map") == 2


@pytest.mark.parametrize("change", ["language", "prompt", "budget"])
def test_returning_to_old_configuration_selects_matching_historical_map_without_fee(
    harness: Harness, change
):
    ctx, ids = setup_map(harness)
    first = generate(ctx)
    changed = change_context(ctx, change, ids[0])
    second = generate(changed)
    assert first.result.id != second.result.id
    plan = topic_map.preview(ctx, TOPIC)
    assert plan.cached_map_id == first.result.id and plan.estimated_usd == 0
    reused = topic_map.queue(ctx, TOPIC, plan.plan_hash)
    assert reused.result == first.result
    assert (
        topic_map.view(ctx, TOPIC).result == first.result and not topic_map.view(ctx, TOPIC).stale
    )
    assert harness.llm_calls("map") == 2
    new_id = add_paper(harness, "New alpha after switching back")
    view = topic_map.view(ctx, TOPIC)
    assert view.result == first.result and not view.stale
    assert view.new_paper_ids == (new_id,) and view.ungrouped == (new_id,)
    assert harness.llm_calls("map") == 2
