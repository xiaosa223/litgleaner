"""生产来源入口真实留档，失败留证且不改变原采纳范围。"""

import json
from dataclasses import replace

import pytest

from litgleaner.application.inbox import add_identifier, add_pdf
from litgleaner.application.references import refresh
from litgleaner.application.stages.discover import DiscoverStage, resolve_record
from litgleaner.application.survey import preview
from litgleaner.domain.citation import ReferenceRecord
from litgleaner.domain.errors import SchemaChanged
from litgleaner.domain.identity import Alias
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.query import JournalWatch
from litgleaner.domain.survey import SurveySettings
from litgleaner.ports.source import FetchResult
from tests.e2e.conftest import Harness, paper
from tests.e2e.source_capture_helpers import CapturingSource, inputs


def _discover(ctx, query=None):
    query = query or JournalWatch(issns=("B", "A", "B"))
    DiscoverStage().process(
        ctx, {"source": "alpha", "query": query.model_dump(mode="json"), "job_key": "frozen-job"}
    )
    return query


def test_discover_persists_original_input_and_ordered_scope_before_facts(harness: Harness):
    harness.write_papers([paper("Captured")])
    query = JournalWatch(issns=("B", "A", "B"))
    harness.ctx.store.papers.set_cursor("alpha", query.hash, "", harness.ctx.now_iso())
    _discover(harness.ctx, query)
    (attempt,) = harness.ctx.store.source_captures.list("alpha")
    assert attempt.query["issns"] == ["B", "A", "B"] and attempt.cursor == ""
    assert attempt.status == "returned" and attempt.finished_at == harness.ctx.now_iso()
    assert attempt.result == {
        "records": 1,
        "references": 0,
        "citations": 0,
        "next_cursor": "1",
        "truncated": False,
    }
    assert attempt.scope.purpose == "discover"
    assert attempt.scope.owner["job_key"] == "frozen-job"
    assert attempt.scope.owner["members"][0]["cursor"] == ""
    assert attempt.recipe.adapter == "fake_source" and attempt.recipe.credentials == {}
    assert attempt.recipe.option_fields == ("records_file",)
    assert json.loads(inputs(harness.ctx, attempt)[0][1])[0]["title"] == "Captured"
    assert harness.ctx.store.stats()["paper"] == 1 and harness.llm_calls() == 0


def test_bad_json_is_durable_before_parse_and_cursor_does_not_move(harness: Harness):
    (harness.root / "config/demo/papers.json").write_bytes(b'{"broken":')
    query = JournalWatch(issns=("A",))
    harness.ctx.store.papers.set_cursor("alpha", query.hash, "0", harness.ctx.now_iso())
    with pytest.raises(json.JSONDecodeError):
        _discover(harness.ctx, query)
    (attempt,) = harness.ctx.store.source_captures.list("alpha")
    assert attempt.status == "raised" and attempt.error_type == "JSONDecodeError"
    assert attempt.result is None and inputs(harness.ctx, attempt) == [("file", b'{"broken":')]
    assert harness.ctx.store.papers.get_cursor("alpha", query.hash) == "0"
    assert harness.ctx.store.stats()["paper"] == harness.llm_calls() == 0


@pytest.mark.parametrize("failure", ["blob", "index", "finish"])
def test_capture_storage_failure_prevents_fact_adoption(harness, monkeypatch, failure):
    harness.write_papers([paper("Unadopted")])
    ctx = harness.ctx
    target = ctx.blobs if failure == "blob" else ctx.store.source_captures
    method = {"blob": "put", "index": "append", "finish": "finish"}[failure]

    def fail(*_args, **_kwargs):
        raise OSError("private storage failure")

    monkeypatch.setattr(target, method, fail)
    with pytest.raises(OSError):
        _discover(ctx)
    (attempt,) = ctx.store.source_captures.list("alpha")
    assert attempt.status == ("started" if failure == "finish" else "raised")
    assert (attempt.error_type is None) == (failure == "finish")
    assert len(inputs(ctx, attempt)) == (1 if failure == "finish" else 0)
    assert ctx.store.stats()["paper"] == ctx.store.stats()["sighting"] == 0
    assert ctx.store.papers.get_cursor("alpha", JournalWatch(issns=("B", "A", "B")).hash) is None
    assert harness.llm_calls() == 0


def test_business_rollback_keeps_returned_capture_and_all_inputs(harness, monkeypatch):
    harness.write_papers([paper("Rollback")])

    def fail(*_args):
        raise RuntimeError("business commit failed")

    monkeypatch.setattr(harness.ctx.store.papers, "record_sighting", fail)
    with pytest.raises(RuntimeError):
        _discover(harness.ctx)
    (attempt,) = harness.ctx.store.source_captures.list("alpha")
    assert attempt.status == "returned" and len(inputs(harness.ctx, attempt)) == 1
    assert harness.ctx.store.stats()["paper"] == 0
    assert harness.ctx.store.papers.get_cursor("alpha", attempt.query_hash) is None


def test_failure_records_only_error_class_and_retry_creates_distinct_attempt(harness):
    source = CapturingSource("bad", error=SchemaChanged("credential=private-detail"))
    ctx = replace(harness.ctx, sources={source.name: source})
    for _ in range(2):
        item = add_identifier(ctx, "10.1000/unseen")
        assert item.status == "failed"
    attempts = ctx.store.source_captures.list("bad")
    assert len(attempts) == 2 and attempts[0].id != attempts[1].id
    assert attempts[0].scope.owner["group_id"] != attempts[1].scope.owner["group_id"]
    assert all(a.status == "raised" and a.error_type == "SchemaChanged" for a in attempts)
    assert all("private-detail" not in repr(a) for a in attempts)
    assert all(a.recipe is None and len(inputs(ctx, a)) == 1 for a in attempts)


def test_replaced_same_name_source_does_not_borrow_previous_recipe(harness):
    source = CapturingSource("alpha")
    ctx = replace(harness.ctx, sources={source.name: source})
    add_identifier(ctx, "10.1000/unknown")
    (attempt,) = ctx.store.source_captures.list("alpha")
    assert attempt.recipe is None and attempt.scope.purpose == "inbox"


def test_interrupted_source_keeps_started_attempt_and_received_input(harness):
    source = CapturingSource("interrupted", error=KeyboardInterrupt())
    ctx = replace(harness.ctx, sources={source.name: source})
    with pytest.raises(KeyboardInterrupt):
        add_identifier(ctx, "10.1000/unknown")
    (attempt,) = ctx.store.source_captures.list("interrupted")
    assert attempt.status == "started" and attempt.finished_at is attempt.error_type is None
    assert len(inputs(ctx, attempt)) == 1 and ctx.store.stats()["paper"] == 0


def test_inbox_preserves_source_order_first_record_and_real_owner(harness):
    result = FetchResult((SourceRecord(**paper("First")), SourceRecord(**paper("Second"))), None)
    empty, chosen, later = (CapturingSource(name) for name in ("empty", "chosen", "later"))
    chosen.result = result
    ctx = replace(harness.ctx, sources={s.name: s for s in (empty, chosen, later)})
    item = add_identifier(ctx, "10.1000/first")
    first, second = (ctx.store.source_captures.list(name)[0] for name in ("empty", "chosen"))
    assert not ctx.store.source_captures.list("later") and not later.calls
    assert first.scope.owner["group_id"] == second.scope.owner["group_id"]
    assert [first.scope.owner["member_index"], second.scope.owner["member_index"]] == [0, 1]
    assert [m["source"] for m in first.scope.owner["members"]] == ["empty", "chosen", "later"]
    assert first.scope.owner["inbox_id"] == item.id and first.scope.owner["inbox_kind"] == "doi"
    assert second.scope.owner["selection"] == "first_nonempty_first_record"
    assert second.scope.owner["identifier_value"] == "10.1000/first"
    assert ctx.store.papers.get(item.paper_id).title == "First"
    assert not ctx.store.papers.find_ids_by_aliases((Alias("doi", "10.1000/second"),))
    assert ctx.store.stats()["paper"] == 1 and harness.llm_calls() == 0


def test_pdf_fallback_scope_keeps_owner_hash_and_title(harness):
    source = CapturingSource("lookup")
    ctx = replace(harness.ctx, sources={source.name: source})
    item = add_pdf(ctx, "folder/Original title.pdf", b"%PDF-test", "10.1000/missing")
    (attempt,) = ctx.store.source_captures.list("lookup")
    owner = attempt.scope.owner
    assert owner["inbox_id"] == item.id and owner["inbox_kind"] == "pdf"
    assert owner["pdf_sha256"] == item.pdf_sha256 and owner["fallback_title"] == "Original title"
    assert item.status == "done" and ctx.store.papers.get(item.paper_id).title == "Original title"


def test_reference_capture_does_not_adopt_returned_candidate_papers(harness):
    ctx = harness.ctx
    parent = resolve_record(ctx.store.papers, SourceRecord(**paper("Parent")), ctx.now_iso())
    source = CapturingSource(
        "refs",
        FetchResult(
            (SourceRecord(**paper("Candidate")),),
            None,
            references=(ReferenceRecord("10.1000/parent", 0, "Original citation", None, ()),),
        ),
    )
    ctx = replace(ctx, sources={source.name: source})
    report = refresh(ctx, parent)
    (attempt,) = ctx.store.source_captures.list("refs")
    assert attempt.scope.purpose == "references" and attempt.scope.owner["paper_id"] == parent
    assert attempt.scope.owner["dois"] == ["10.1000/parent"]
    assert report.sources[0].added == 1 and ctx.store.stats()["paper"] == 1
    assert attempt.result["records"] == 1 and len(inputs(ctx, attempt)) == 1
    assert harness.llm_calls() == 0


def test_survey_freezes_complete_group_plan_limits_and_failure_members(harness):
    candidate = SourceRecord(**paper("Candidate alpha"))
    source = CapturingSource("good", FetchResult((candidate,), None))
    failed = CapturingSource("failed", error=SchemaChanged("sensitive"))
    ctx = replace(harness.ctx, sources={s.name: s for s in (source, failed)})
    ctx = replace(ctx, settings=replace(ctx.settings, survey=SurveySettings(candidate_limit=1)))
    plan = preview(ctx, "alpha-topic")
    attempts = tuple(ctx.store.source_captures.list(s.name) for s in (failed, source))
    assert [len(items) for items in attempts] == [3, 3]
    all_attempts = [a for items in attempts for a in items]
    owners = [a.scope.owner for a in all_attempts]
    assert len({o["group_id"] for o in owners}) == 1
    assert sorted(o["member_index"] for o in owners) == list(range(6))
    assert all(o["plan_id"] == plan.id and o["topic_hash"] == plan.topic_hash for o in owners)
    assert all(o["settings"]["candidate_limit"] == 1 and len(o["members"]) == 6 for o in owners)
    assert all(o["run_id"] is None and o["iteration"] is None for o in owners)
    assert owners[0]["today"] == "2026-11-03" and owners[0]["timezone"] == "Asia/Shanghai"
    assert owners[0]["topic"]["query"]["include"] == ["alpha"]
    assert all(a.status == "raised" for a in attempts[0])
    assert all(a.status == "returned" for a in attempts[1])
    assert len(plan.candidates) == 1 and ctx.store.stats()["paper"] == harness.llm_calls() == 0
    assert all(ctx.store.papers.get_cursor(a.source, a.query_hash) is None for a in all_attempts)
    assert not ctx.store.jobs.due("survey", ctx.now_iso())


def test_survey_internal_capture_keeps_explicit_run_and_iteration(harness):
    source = CapturingSource("good", FetchResult((SourceRecord(**paper("alpha")),), None))
    ctx = replace(harness.ctx, sources={source.name: source})
    plan = preview(ctx, "alpha-topic", internal=True, run_id="original-run", iteration=2)
    (attempt, *_) = ctx.store.source_captures.list("good")
    assert attempt.scope.owner["run_id"] == "original-run"
    assert attempt.scope.owner["iteration"] == 2 and attempt.scope.owner["internal"]
    assert attempt.scope.owner["plan_id"] == plan.id
