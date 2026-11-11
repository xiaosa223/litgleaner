"""调研计划消费、工作恢复、并发轮次和首次内容保留。"""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import Paper, SourceRecord
from litgleaner.domain.survey import (
    NewSurveyRound,
    SurveyCandidate,
    SurveyEdge,
    SurveyPreview,
    SurveySettings,
    SurveySource,
    SurveyWork,
)
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import survey_preview, survey_round, survey_work
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import JobSpec


def preview(plan_id="plan", topic_id="topic"):
    source = SurveySource(
        source="source",
        mode="reviews",
        query_hash="q",
        fetched=1,
        truncated=True,
        warnings=("Original warning",),
        raw_key="raw/one.zst",
    )
    candidate = SurveyCandidate(
        key="key",
        record=SourceRecord(title="Original", doi="10.1000/a"),
        sources=("source",),
        modes=("reviews",),
    )
    edge = SurveyEdge(
        source="source",
        citing_aliases=(Alias("doi", "10.1000/a"),),
        cited_aliases=(Alias("doi", "10.1000/b"),),
        source_position=0,
        raw_text="Original reference",
        contexts=("Original context",),
        influential=True,
    )
    return SurveyPreview(
        id=plan_id,
        topic_id=topic_id,
        topic_hash="hash",
        fingerprint="fingerprint",
        created_at="t",
        settings=SurveySettings(),
        candidates=(candidate,),
        edges=(edge,),
        sources=(source,),
        uncovered=("citations",),
        triage_calls=1,
        locally_rejected=0,
        known_papers=0,
        estimated_usd=0.01,
        estimated_pipeline_usd=0.5,
    )


def work(run_id="run", iteration=1, plan_id="plan", **changes):
    values = {
        "run_id": run_id,
        "iteration": iteration,
        "plan_id": plan_id,
        "scope_id": f"{run_id}:{iteration}",
        "topic_id": "topic",
        "topic_hash": "hash",
        "started_at": "t",
        "paper_ids": ("P",),
        "new_paper_ids": ("P",),
        "paper_sources": {"P": ("source",)},
        "rejected_records": 0,
    }
    return SurveyWork(**(values | changes))


def round_item(run_id="run", iteration=1, plan_id="plan", **changes):
    values = {
        "topic_id": "topic",
        "topic_hash": "hash",
        "run_id": run_id,
        "iteration": iteration,
        "plan_id": plan_id,
        "started_at": "t",
        "finished_at": "t2",
        "candidates": 1,
        "new_papers": 1,
        "passed": 1,
        "new_passed": 1,
        "undetermined": 0,
        "cost_usd": 0.1,
        "coverage": 1,
        "next_recommended": True,
        "contributions": {"source": 1},
        "paper_ids": ("P",),
    }
    return NewSurveyRound(**(values | changes))


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'survey.db'}"
    upgrade_database(url)
    result = SqlStore(url)
    result.papers.insert(Paper("P", "Original", "original", None, (), None, None, None, "t"))
    yield result
    result.dispose()


def _accepted(store, plan_id="plan", run_id="run", *, topic_id="topic"):
    assert store.surveys.add_preview(preview(plan_id, topic_id))
    assert store.surveys.accept(plan_id, run_id, "t")


def test_preview_roundtrip_first_json_and_atomic_consumption_preserve_original_content(store):
    first = preview()
    assert store.surveys.add_preview(first)
    assert not store.surveys.add_preview(
        first.model_copy(update={"candidates": (), "fingerprint": "new"})
    )
    assert store.surveys.preview("plan") == first
    assert store.surveys.preview("missing") is store.surveys.accepted_run("plan") is None
    assert not store.surveys.accept("missing", "run", "t")
    assert store.surveys.accept("plan", "first-run", "t")
    assert not store.surveys.accept("plan", "second-run", "t2")
    assert not store.surveys.accept("plan", "first-run", "t3")
    assert store.surveys.accepted_run("plan") == "first-run"
    assert store.surveys.preview("plan") == first


def test_accept_can_share_enqueue_transaction_and_rolls_back_on_error(store):
    store.surveys.add_preview(preview())
    with pytest.raises(RuntimeError), store.atomic():
        assert store.surveys.accept("plan", "run", "t")
        assert store.jobs.enqueue(JobSpec("survey:run", "survey", {}), "t")
        raise RuntimeError("abort")
    assert store.surveys.accepted_run("plan") is None
    assert store.jobs.get("survey:run") is None


def test_concurrent_accept_preserves_one_winner_and_original_plan(store):
    store.surveys.add_preview(preview())
    barrier = Barrier(4)

    def accept(index):
        barrier.wait(timeout=5)
        return store.surveys.accept("plan", f"run-{index}", "t")

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(accept, range(4))) == 1
    assert store.surveys.accepted_run("plan") in {f"run-{index}" for index in range(4)}
    assert store.surveys.preview("plan") == preview()


def test_work_and_round_roundtrip_are_idempotent_and_first_write_keeps_facts(store):
    _accepted(store)
    initial = work()
    assert store.surveys.save_work(initial)
    assert not store.surveys.save_work(
        initial.model_copy(update={"paper_ids": (), "scope_id": "different"})
    )
    assert store.surveys.get_work("run", 1) == initial
    result = store.surveys.append_round(round_item())
    assert result.number == 1 and result.cost_usd == 0.1
    assert store.surveys.append_round(round_item(cost_usd=9, cancelled=True)) == result
    assert store.surveys.round_for("run", 1) == result
    assert store.surveys.rounds("topic") == (result,)
    assert store.surveys.rounds("missing") == ()
    assert store.surveys.get_work("missing", 1) is store.surveys.round_for("run", 99) is None


@pytest.mark.parametrize(
    "change",
    [{"run_id": "wrong"}, {"plan_id": "missing"}, {"topic_hash": "old"}, {"topic_id": "other"}],
)
def test_work_rejects_unaccepted_or_mismatched_plan(store, change):
    _accepted(store)
    with pytest.raises(ValueError, match="survey_plan_missing_or_mismatched"):
        store.surveys.save_work(work(**change))


def test_round_requires_matching_saved_work_and_real_paper_links(store):
    _accepted(store)
    with pytest.raises(ValueError, match="survey_work_missing"):
        store.surveys.append_round(round_item())
    with pytest.raises(ValueError, match="survey_paper_missing"):
        store.surveys.save_work(
            work(paper_ids=("missing",), new_paper_ids=("missing",), paper_sources={})
        )
    assert store.surveys.save_work(work())
    with pytest.raises(ValueError, match="survey_paper_missing"):
        store.surveys.append_round(round_item(paper_ids=("missing",)))
    assert store.surveys.rounds("topic") == ()


def test_parallel_round_allocation_is_topic_monotonic_and_same_iteration_preserves_winner(store):
    _accepted(store)
    for iteration in range(1, 5):
        store.surveys.save_work(work(iteration=iteration))
    barrier = Barrier(4)

    def append(iteration):
        barrier.wait(timeout=5)
        return store.surveys.append_round(round_item(iteration=iteration))

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(append, range(1, 5)))
    assert {item.number for item in values} == {1, 2, 3, 4}
    assert [item.number for item in store.surveys.rounds("topic", 2)] == [3, 4]
    _accepted(store, "second", "second-run", topic_id="other")
    store.surveys.save_work(work("second-run", plan_id="second", topic_id="other"))
    assert (
        store.surveys.append_round(
            round_item("second-run", plan_id="second", topic_id="other")
        ).number
        == 1
    )
    store.surveys.save_work(work(iteration=5))
    barrier = Barrier(4)

    def duplicate(cost):
        barrier.wait(timeout=5)
        return store.surveys.append_round(round_item(iteration=5, cost_usd=cost))

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len({item.model_dump_json() for item in pool.map(duplicate, range(4))}) == 1
    assert len(store.surveys.rounds("topic")) == 5


@pytest.mark.parametrize("table", [survey_preview, survey_work, survey_round])
def test_immutable_data_and_plan_or_work_foreign_keys_cannot_be_bypassed(store, table):
    _accepted(store)
    store.surveys.save_work(work())
    store.surveys.append_round(round_item())
    with pytest.raises(IntegrityError, match="immutable"):
        store._db.write(update(table).values(data="{}"))
    if table != survey_round:
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            store._db.write(delete(table))


@pytest.mark.parametrize("limit", [0, -1, True])
def test_round_limit_is_positive_and_strict(store, limit):
    with pytest.raises(ValueError, match="survey_limit_invalid"):
        store.surveys.rounds("topic", limit)


def test_first_round_and_work_rollback_with_outer_transaction(store):
    _accepted(store)
    with pytest.raises(RuntimeError), store.atomic():
        store.surveys.save_work(work())
        store.surveys.append_round(round_item())
        raise RuntimeError("abort")
    assert store.surveys.get_work("run", 1) is store.surveys.round_for("run", 1) is None
    store.surveys.save_work(work())
    assert store.surveys.append_round(round_item()).number == 1


def test_parallel_work_saves_preserve_one_complete_winner(store):
    _accepted(store)
    barrier = Barrier(4)

    def save(index):
        barrier.wait(timeout=5)
        return store.surveys.save_work(work(scope_id=f"scope-{index}"))

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(save, range(4))) == 1
    assert store.surveys.get_work("run", 1).scope_id in {f"scope-{i}" for i in range(4)}


def test_consumed_plan_cannot_be_rebound_through_direct_sql(store):
    _accepted(store)
    with pytest.raises(IntegrityError, match="survey_preview_immutable"):
        store._db.write(update(survey_preview).values(accepted_run="other", accepted_at="later"))
    assert store.surveys.accepted_run("plan") == "run"


def test_latest_preview_is_topic_scoped_stably_orders_ties_and_keeps_accepted_plan(store):
    for item in (
        preview("older").model_copy(update={"created_at": "2026-01-01"}),
        preview("a").model_copy(update={"created_at": "2026-01-02"}),
        preview("z").model_copy(update={"created_at": "2026-01-02"}),
        preview("other", "other").model_copy(update={"created_at": "2027-01-01"}),
    ):
        assert store.surveys.add_preview(item)
    assert store.surveys.accept("z", "first-run", "t")
    assert store.surveys.latest_preview("topic").id == "z"
    assert store.surveys.latest_preview("other").id == "other"
    assert store.surveys.latest_preview("missing") is None


def test_append_only_round_cannot_be_deleted_to_reuse_its_number(store):
    _accepted(store)
    store.surveys.save_work(work())
    original = store.surveys.append_round(round_item())
    with pytest.raises(IntegrityError, match="survey_round_immutable"):
        store._db.write(delete(survey_round))
    assert store.surveys.round_for("run", 1) == original
    store.surveys.save_work(work(iteration=2))
    assert store.surveys.append_round(round_item(iteration=2)).number == 2


@pytest.mark.parametrize(
    "change",
    [
        {"iteration": 0},
        {"number": 0},
        {"candidates": -1},
        {"new_passed": 2},
        {"passed": 1.5},
        {"undetermined": 2},
        {"cost_usd": -0.1},
        {"run_id": ""},
    ],
)
def test_database_rejects_invalid_round_identifiers_counts_and_types(store, change):
    _accepted(store)
    store.surveys.save_work(work())
    content = round_item().model_dump(mode="json") | {"number": 1} | change
    names = (
        "topic_id",
        "number",
        "topic_hash",
        "run_id",
        "iteration",
        "plan_id",
        "started_at",
        "finished_at",
        "candidates",
        "new_papers",
        "passed",
        "new_passed",
        "undetermined",
        "cost_usd",
    )
    values = {key: content[key] for key in names}
    with pytest.raises(IntegrityError):
        store._db.write(survey_round.insert().values(**values, data=json.dumps(content)))
