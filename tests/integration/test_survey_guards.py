"""调研暂停/失败保留论文所有权，通用重试不能复活终态子任务。"""

import json

import pytest
from sqlalchemy import event, text

from litgleaner.domain.survey import NewSurveyRound, SurveyPreview, SurveySettings, SurveyWork
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import survey_preview
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import JobSpec


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'guards.db'}"
    upgrade_database(url)
    result = SqlStore(url)
    result._db.write(
        text(
            "INSERT INTO paper(id,title,title_key,authors,first_seen_at) "
            "VALUES ('P1','First','first','[]','t'),('P2','Second','second','[]','t')"
        )
    )
    yield result
    result.dispose()


def _preview(store, plan_id, *, topic="topic", created_at="t", internal=False):
    item = SurveyPreview(
        id=plan_id,
        topic_id=topic,
        topic_hash="hash",
        fingerprint="f",
        created_at=created_at,
        settings=SurveySettings(),
        candidates=(),
        triage_calls=0,
        locally_rejected=0,
        known_papers=0,
    )
    # 未发布契约由 root 同时增加；明确写入 JSON 以兼容旧无标记计划。
    values = item.model_dump(mode="json") | {"internal": internal}
    columns = ("id", "topic_id", "topic_hash", "fingerprint", "created_at")
    store._db.write(
        survey_preview.insert().values(
            **{key: values[key] for key in columns}, data=json.dumps(values)
        )
    )


def _work(store, run="run", iteration=1, *, topic="topic", papers=("P1",), status="pending"):
    plan = f"{run}:{iteration}"
    _preview(store, plan, topic=topic)
    assert store.surveys.accept(plan, run, "t")
    store.jobs.enqueue(JobSpec(f"survey:{run}", "survey", {}), "t")
    item = SurveyWork(
        run_id=run,
        iteration=iteration,
        plan_id=plan,
        scope_id=plan,
        topic_id=topic,
        topic_hash="hash",
        started_at="t",
        paper_ids=papers,
        new_paper_ids=papers,
        paper_sources={},
    )
    assert store.surveys.save_work(item)
    if status == "done":
        store.jobs.mark_done(f"survey:{run}", "t")
    elif status == "dead":
        store.jobs.mark_dead(f"survey:{run}", "failure", "t")
    return item


def _round(store, item, *, cancelled=False, failed=(), papers=None):
    selected = item.paper_ids if papers is None else papers
    return store.surveys.append_round(
        NewSurveyRound(
            topic_id=item.topic_id,
            topic_hash=item.topic_hash,
            run_id=item.run_id,
            iteration=item.iteration,
            plan_id=item.plan_id,
            started_at="t",
            finished_at="t2",
            candidates=len(item.paper_ids),
            new_papers=len(item.new_paper_ids),
            passed=0,
            new_passed=0,
            undetermined=0,
            cost_usd=0,
            paper_ids=selected,
            cancelled=cancelled,
            failed_jobs=failed,
        )
    )


@pytest.mark.parametrize("status", ["pending", "dead"])
def test_pending_or_dead_parent_holds_every_iteration_globally_even_with_success_round(
    store, status
):
    first = _work(store, papers=("P1",), status=status)
    _round(store, first)
    _work(store, iteration=2, papers=("P2",), status=status)
    assert store.surveys.held_paper_ids() == {"P1", "P2"}


@pytest.mark.parametrize("cancelled,failed", [(True, ()), (False, ("survey_child:failed",))])
def test_done_cancelled_or_failed_round_holds_work_papers_until_later_same_topic_success(
    store, cancelled, failed
):
    old = _work(store, papers=("P1", "P2"), status="done")
    _round(store, old, cancelled=cancelled, failed=failed, papers=())
    assert store.surveys.held_paper_ids() == {"P1", "P2"}
    outsider = _work(store, "other", topic="other", status="done")
    _round(store, outsider)
    assert store.surveys.held_paper_ids() == {"P1", "P2"}
    newer_failed = _work(store, "new-failed", status="done")
    _round(store, newer_failed, failed=("failed",))
    assert store.surveys.held_paper_ids() == {"P1", "P2"}
    newer = _work(store, "new-success", status="done")
    _round(store, newer)
    assert store.surveys.held_paper_ids() == {"P2"}


def test_old_success_cannot_release_new_failure_and_done_success_is_not_held(store):
    first = _work(store, "first", status="done")
    _round(store, first)
    assert store.surveys.held_paper_ids() == frozenset()
    later = _work(store, "later", status="done")
    _round(store, later, cancelled=True)
    assert store.surveys.held_paper_ids() == {"P1"}


def test_one_live_work_keeps_paper_held_despite_later_success_releasing_old_failure(store):
    failed = _work(store, "failed", status="done")
    _round(store, failed, cancelled=True)
    _work(store, "live")
    success = _work(store, "success", status="done")
    _round(store, success)
    assert store.surveys.held_paper_ids() == {"P1"}


def test_hold_uses_one_bulk_query_for_more_than_999_papers_without_expanding_bindings(store):
    store._db.write(
        text(
            "WITH RECURSIVE ids(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM ids WHERE n<1001) "
            "INSERT INTO paper(id,title,title_key,authors,first_seen_at) "
            "SELECT 'bulk-'||n,'T','t','[]','t' FROM ids"
        )
    )
    papers = tuple(f"bulk-{index}" for index in range(1, 1002))
    _work(store, papers=papers)
    statements = []

    def record(_connection, _cursor, statement, params, _context, _many):
        statements.append((statement, len(params)))

    event.listen(store._db._engine, "before_cursor_execute", record)
    try:
        assert store.surveys.held_paper_ids() == frozenset(papers)
    finally:
        event.remove(store._db._engine, "before_cursor_execute", record)
    assert len(statements) == 1 and statements[0][1] == 0


def _child(store, key, run="run", iteration=1, *, payload=None):
    markers = {"survey_run_id": run, "survey_iteration": iteration} if payload is None else payload
    store.jobs.enqueue(JobSpec(key, "survey_child", markers), "t")
    store.jobs.mark_dead(key, "child failure", "t")


@pytest.mark.parametrize("status", ["done", "dead", "missing"])
def test_retry_dead_does_not_revive_child_of_nonpending_or_missing_parent(store, status):
    if status != "missing":
        _work(store, status=status)
    _child(store, "child")
    count = store.jobs.retry_dead("later")
    assert count == (1 if status == "dead" else 0)  # 通用父任务可重试，本次不能借机复活 child。
    assert store.jobs.get("child").status == "dead"


def test_retry_allows_only_live_pending_parent_unfinished_iteration_and_keeps_other_stages(store):
    first = _work(store)
    _round(store, first)
    _work(store, iteration=2)
    _child(store, "terminal", iteration=1)
    _child(store, "live", iteration=2)
    store.jobs.enqueue(JobSpec("ordinary", "triage", {}), "t")
    store.jobs.mark_dead("ordinary", "failure", "t")
    store.jobs.postpone("live", "deferred", "t2", "t")
    store.jobs.mark_dead("live", "failed again", "t")
    assert store.jobs.retry_dead("later") == 2
    assert store.jobs.get("terminal").status == "dead"
    for key in ("live", "ordinary"):
        found = store.jobs.get(key)
        assert found.status == "pending" and found.attempts == 0 and found.first_deferred_at is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"survey_run_id": "run"},
        {"survey_run_id": "", "survey_iteration": 1},
        {"survey_run_id": 1, "survey_iteration": 1},
        {"survey_run_id": "run", "survey_iteration": True},
        {"survey_run_id": "run", "survey_iteration": "1"},
        {"survey_run_id": "run", "survey_iteration": 0},
    ],
)
def test_retry_rejects_missing_or_malformed_survey_child_markers(store, payload):
    _work(store)
    _child(store, "child", payload=payload)
    assert store.jobs.retry_dead("later") == 0
    assert store.jobs.get("child").status == "dead"


def test_latest_preview_ignores_internal_but_keeps_accepted_and_legacy_unmarked(store):
    _preview(store, "user", created_at="2026-01-01")
    _preview(store, "private", created_at="2027-01-01", internal=True)
    assert store.surveys.accept("user", "run", "t")
    assert store.surveys.latest_preview("topic").id == "user"
    assert store.surveys.preview("private").id == "private"
    legacy = SurveyPreview(
        id="legacy",
        topic_id="other",
        topic_hash="h",
        fingerprint="f",
        created_at="t",
        settings=SurveySettings(),
        candidates=(),
        triage_calls=0,
        locally_rejected=0,
        known_papers=0,
    )
    values = legacy.model_dump(mode="json")
    values.pop("internal", None)
    store._db.write(
        survey_preview.insert().values(
            id=legacy.id,
            topic_id=legacy.topic_id,
            topic_hash=legacy.topic_hash,
            fingerprint=legacy.fingerprint,
            created_at=legacy.created_at,
            data=json.dumps(values),
        )
    )
    assert store.surveys.latest_preview("other").id == "legacy"
