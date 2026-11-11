"""地图与综述报告沿真实视图选择历史，仅冻结来源变化才失效。"""

from dataclasses import replace

import pytest
from sqlalchemy import update

from litgleaner.application import outline, topic_export, topic_map
from litgleaner.application.pipeline import has_on_demand_work
from litgleaner.application.stages.assess import assessment_input_hash
from litgleaner.application.stale import report
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.domain.rubric import ScoreResult
from litgleaner.infra.db.schema import paper
from litgleaner.ports.store import NewAssessment
from tests.e2e.conftest import Harness
from tests.e2e.outline_helpers import add_read_paper, setup_outline
from tests.e2e.outline_helpers import generate as generate_outline
from tests.e2e.topic_map_helpers import TOPIC, add_paper, setup_map


def _generate(ctx):
    queued = topic_map.queue(ctx, TOPIC, topic_map.preview(ctx, TOPIC).plan_hash)
    outlined = generate_outline(ctx)
    mapped = topic_map.status(ctx, TOPIC, queued.job_key)
    assert mapped.status == outlined.status == "done"
    assert mapped.result is not None and outlined.result is not None
    return mapped.result, outlined.result


@pytest.fixture
def saved(harness: Harness):
    setup_map(harness, count=0)
    ctx, ids = setup_outline(harness, count=5)
    mapped, outlined = _generate(ctx)
    return ctx, ids, mapped, outlined


def _entries(ctx):
    return {
        (entry.kind, entry.id): entry
        for entry in report(ctx).items
        if entry.kind in ("map", "outline")
    }


def _set_title(ctx, paper_id, title):
    ctx.store._db.write(update(paper).where(paper.c.id == paper_id).values(title=title))


def test_current_map_and_outline_are_selected_without_new_work(harness: Harness, saved):
    ctx, _ids, mapped, outlined = saved
    calls = harness.llm_calls()
    entries = _entries(ctx)
    assert set(entries) == {("map", mapped.id), ("outline", outlined.id)}
    for (kind, id_), entry in entries.items():
        assert entry.status == "current" and entry.selected is True
        assert entry.reasons == entry.unchecked == ()
        view = topic_map.view(ctx, TOPIC) if kind == "map" else outline.view(ctx, TOPIC)
        assert view.result.id == id_ and not view.stale
    assert harness.llm_calls() == calls and not has_on_demand_work(ctx)


def test_older_matching_source_history_is_selected_ahead_of_newer_mismatch(harness: Harness, saved):
    ctx, ids, first_map, first_outline = saved
    original = ctx.store.papers.get(ids[0])
    _set_title(ctx, original.id, "Revised alpha title")
    harness.clock.advance(seconds=1)
    second_map, second_outline = _generate(ctx)
    assert ctx.store.topic_maps.latest(TOPIC) == second_map
    assert ctx.store.topic_outlines.latest(TOPIC) == second_outline
    _set_title(ctx, original.id, original.title)
    calls = harness.llm_calls()
    entries = _entries(ctx)
    for kind, older, newer, view in (
        ("map", first_map, second_map, topic_map.view(ctx, TOPIC)),
        ("outline", first_outline, second_outline, outline.view(ctx, TOPIC)),
    ):
        assert newer.created_at > older.created_at and newer.source_hashes != older.source_hashes
        assert entries[kind, older.id].status == "current"
        assert entries[kind, older.id].selected is True
        assert entries[kind, newer.id].status == "stale"
        assert entries[kind, newer.id].selected is False
        assert view.result == older and not view.stale
    assert harness.llm_calls() == calls and not has_on_demand_work(ctx)


def test_added_papers_leave_frozen_subsets_current_and_match_existing_views(
    harness: Harness, saved
):
    ctx, ids, mapped, outlined = saved
    unread = add_paper(harness, "Later unread alpha")
    read = add_read_paper(harness, "Later read alpha", evidence=False)
    calls = harness.llm_calls()
    entries = _entries(ctx)
    assert all(entry.status == "current" and entry.selected for entry in entries.values())
    mapped_view, outlined_view = topic_map.view(ctx, TOPIC), outline.view(ctx, TOPIC)
    assert mapped_view.result == mapped and outlined_view.result == outlined
    assert set(mapped_view.new_paper_ids) == {unread, read}
    assert outlined_view.new_paper_ids == (read,)
    assert not mapped_view.stale and not outlined_view.stale
    assert mapped.input_paper_ids == outlined.input_paper_ids == tuple(sorted(ids))
    assert harness.llm_calls() == calls and not has_on_demand_work(ctx)


@pytest.mark.parametrize("change", ["extraction", "unread"])
def test_changed_sent_source_or_withdrawn_reading_marks_only_affected_results_stale(
    harness: Harness, saved, change
):
    ctx, ids, mapped, outlined = saved
    if change == "extraction":
        ctx.store.papers.insert_extraction(
            ids[0],
            "changed-input",
            "changed-extractor",
            {"summary": "Changed source"},
            ctx.now_iso(),
        )
    else:
        ctx.store.reader.set_state(ids[0], "reading", {}, ctx.now_iso())
    calls = harness.llm_calls()
    entries = _entries(ctx)
    for kind, result, view in (
        ("map", mapped, topic_map.view(ctx, TOPIC)),
        ("outline", outlined, outline.view(ctx, TOPIC)),
    ):
        stale = change == "extraction" or kind == "outline"
        entry = entries[kind, result.id]
        assert entry.status == ("stale" if stale else "current") and entry.selected is True
        assert view.result == result and view.stale is stale
        assert bool(entry.reasons) is stale
        if stale:
            assert "来源" in " ".join(entry.reasons)
    assert harness.llm_calls() == calls and not has_on_demand_work(ctx)


@pytest.mark.parametrize("task", ["map", "outline"])
def test_missing_task_route_is_unknown_while_latest_result_remains_selected(saved, task):
    ctx, _ids, mapped, outlined = saved
    offline = replace(
        ctx,
        llm=replace(
            ctx.llm, routes={name: route for name, route in ctx.llm.routes.items() if name != task}
        ),
    )
    entries = _entries(offline)
    for kind, result in (("map", mapped), ("outline", outlined)):
        entry = entries[kind, result.id]
        assert entry.status == ("unknown" if kind == task else "current")
        assert entry.selected is True and entry.reasons == ()
        assert bool(entry.unchecked) is (kind == task)
        if kind == task:
            assert "本任务模型" in " ".join(entry.unchecked)
    view = topic_map.view(offline, TOPIC) if task == "map" else outline.view(offline, TOPIC)
    assert view.result.id == (mapped.id if task == "map" else outlined.id)


def test_removed_topic_is_unknown_and_no_saved_result_is_selected(saved):
    ctx, _ids, mapped, outlined = saved
    entries = _entries(replace(ctx, topics={}))
    assert set(entries) == {("map", mapped.id), ("outline", outlined.id)}
    assert all(entry.status == "unknown" and entry.selected is False for entry in entries.values())
    assert all(
        entry.reasons == () and "课题配置不存在" in " ".join(entry.unchecked)
        for entry in entries.values()
    )


def _assessment(ctx, paper_id, input_hash):
    topic = ctx.topics[TOPIC]
    rubric = ctx.rubrics[topic.rubric]
    ctx.store.judgments.insert_assessment(
        NewAssessment(
            paper_id,
            TOPIC,
            topic.topic_hash,
            rubric.semantic_hash,
            input_hash,
            {dimension.key: 5 for dimension in rubric.dimensions},
            {},
            True,
            "response-model",
            ctx.llm.routes["assess"].prompt.version,
        ),
        ctx.now_iso(),
    )
    return ctx.store.judgments.unscored_assessments(
        TOPIC, rubric.semantic_hash, rubric.policy_hash
    )[-1]


def test_latest_unscored_assessment_cannot_reuse_old_score_or_keep_scored_outline_current(saved):
    ctx, ids, mapped, unscored = saved
    topic = ctx.topics[TOPIC]
    rubric = ctx.rubrics[topic.rubric]
    current_paper = ctx.store.papers.get(ids[0])
    fulltext = ctx.store.papers.get_fulltext(ids[0])
    expected = assessment_input_hash(
        current_paper,
        topic,
        fulltext,
        ctx.settings.chunker,
        reading_language=ctx.settings.reading_language,
        token_budget=ctx.settings.assess_token_budget,
    )
    old_id = _assessment(ctx, ids[0], expected)
    ctx.store.judgments.save_score(
        old_id, rubric.policy_hash, ScoreResult(85, "must_read", False, False), ctx.now_iso()
    )
    scored = generate_outline(ctx).result
    assert "85" in next(item.table_markdown for item in scored.papers if item.paper_id == ids[0])
    assert not outline.view(ctx, TOPIC).stale
    new_id = _assessment(ctx, ids[0], "newer-unscored-input")
    assert new_id > old_id
    basis = StaleInputs(ctx, ctx.store.stale_snapshot())
    exported = topic_export.snapshot(ctx, topic)
    assert basis.topic_snapshot(topic) == exported and exported.scores == {}
    entries = _entries(ctx)
    assert entries["map", mapped.id].status == "current"
    assert entries["outline", scored.id].status == "stale"
    assert entries["outline", scored.id].selected is False
    assert entries["outline", unscored.id].status == "current"
    assert entries["outline", unscored.id].selected is True
    assert outline.view(ctx, TOPIC).result == unscored and not outline.view(ctx, TOPIC).stale
    scores = [item for item in report(ctx).items if item.kind == "score"]
    assert len(scores) == 1 and scores[0].selected is False
