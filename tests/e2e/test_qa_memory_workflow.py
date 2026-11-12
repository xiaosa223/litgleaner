"""问答冻结真实记忆，跨论文出处必须核对且输入改变不能静默切换。"""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from litgleaner.application import qa, relation_feedback
from litgleaner.application import qa_memory_input as inputs
from litgleaner.application import qa_memory_run as memory_run
from litgleaner.application import qa_memory_state as memory_state
from litgleaner.application.llm_gateway import Prompt, TaskRoute
from litgleaner.application.pipeline import run_on_demand
from litgleaner.application.qa_context import qa_token_count
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.application.stale_language import check_answers
from litgleaner.application.topics import pause_topic
from litgleaner.domain.errors import PermanentError
from litgleaner.domain.reading import NewAnnotation
from litgleaner.server.app import create_app
from litgleaner.server.security import LocalSecurity
from tests.e2e.relate_flow_helpers import generate, respond
from tests.e2e.relate_helpers import HERE, THERE, pair

NOTE = "My attention alignment experiment used a sparse comparison baseline."


@pytest.fixture
def memory_case(harness, monkeypatch):
    a, b = pair(harness)
    respond(harness, monkeypatch)
    generate(harness, a)
    relation = harness.ctx.store.stale_snapshot().relations[0]
    prompt = (harness.root / "prompts/ask.md").read_text()
    route = TaskRoute(harness.llm, "strong", Prompt("ask", prompt), "test-provider")
    harness.ctx = replace(
        harness.ctx, llm=replace(harness.ctx.llm, routes={**harness.ctx.llm.routes, "ask": route})
    )
    note = harness.ctx.store.reader.add_annotation(
        NewAnnotation(b, "note", None, None, None, NOTE), harness.ctx.now_iso()
    )
    harness.llm.overrides["ask"] = {
        "statements": [
            {
                "text": "An own-paper explanation.",
                "citations": [{"paper_id": a, "page": 2, "quote": HERE}],
            }
        ]
    }
    return a, b, relation, note


def _queue(harness, a, request="ask-memory"):
    return qa.queue(harness.ctx, a, "Compare attention alignment experiments", request_id=request)


def test_queue_freezes_complete_memory_without_calling_paid_models(harness, memory_case):
    a, b, relation, note = memory_case
    question = _queue(harness, a)
    job = harness.ctx.store.jobs.get(qa.question_job_key(question.id))
    manifest = job.payload["context"]
    data = json.loads(manifest["inputs"])
    assert manifest["contract"] == "qa-memory-v1" and manifest["producer"]
    assert data["papers"][b] == harness.ctx.store.papers.get(b).title
    assert any(s["source_id"] == note.id and s["text"] == NOTE for s in data["sources"])
    assert {r["id"] for r in manifest["relations"]} == {relation.id}
    assert any(s["paper_id"] == b and THERE in s["text"] for s in data["sources"])
    assert (
        qa_token_count(
            manifest["system"] + "<untrusted_paper>\n" + manifest["inputs"] + "\n</untrusted_paper>"
        )
        <= 4000
    )
    assert harness.llm_calls("ask") == 0


def test_answer_cites_external_author_and_user_note_with_separate_provenance(harness, memory_case):
    a, b, _, note = memory_case
    harness.llm.overrides["ask"] = {
        "statements": [
            {
                "text": f"Compare [[paper:{b}]] with the current result.",
                "citations": [{"paper_id": b, "page": 2, "quote": THERE}],
                "note_citations": [
                    {"paper_id": b, "source_id": note.id, "kind": "note", "quote": NOTE}
                ],
                "mentioned_paper_ids": [b],
            }
        ]
    }
    question = _queue(harness, a)
    assert run_on_demand(harness.ctx).ok
    answer = qa.answer(harness.ctx, question.id)
    statement = answer.statements[0]
    assert statement.citations[0].paper_id == b and statement.citations[0].quote == THERE
    assert statement.note_citations[0].source_id == note.id and not statement.inference
    assert statement.mentioned_paper_ids == (b,)
    assert answer.context and answer.request_producer_version == answer.context["producer"]
    assert harness.llm_calls("ask") == 1
    current = check_answers(StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot()))[0]
    assert current.status == "current" and not current.unchecked


@pytest.mark.parametrize("change", ["note", "delete", "relation", "model", "profile"])
def test_selected_input_change_before_execution_creates_no_model_charge(
    harness, memory_case, change
):
    a, _, relation, note = memory_case
    question = _queue(harness, a)
    if change == "note":
        harness.ctx.store.reader.update_annotation(note.id, "A different personal note.", "later")
    elif change == "delete":
        harness.ctx.store.reader.delete_annotation(note.id, "later")
    elif change == "relation":
        relation_feedback.apply(harness.ctx, relation.id, "reject", "reject-before-ask")
    elif change == "model":
        route = harness.ctx.llm.routes["ask"]
        harness.ctx = replace(
            harness.ctx,
            llm=replace(
                harness.ctx.llm,
                routes={**harness.ctx.llm.routes, "ask": replace(route, model="changed")},
            ),
        )
    else:
        topic = harness.ctx.topics["alpha-topic"]
        harness.ctx = replace(
            harness.ctx,
            topics={topic.id: topic.model_copy(update={"background": "Changed research profile"})},
        )
    assert not run_on_demand(harness.ctx).ok
    job = harness.ctx.store.jobs.get(qa.question_job_key(question.id))
    assert job.status == "dead" and "重新提问" in job.last_error
    assert qa.answer(harness.ctx, question.id) is None and harness.llm_calls("ask") == 0


def test_new_unselected_note_does_not_expand_or_invalidate_frozen_question(harness, memory_case):
    a, b, _, _ = memory_case
    question = _queue(harness, a)
    payload = harness.ctx.store.jobs.get(qa.question_job_key(question.id)).payload
    harness.ctx.store.reader.add_annotation(
        NewAnnotation(b, "note", None, None, None, "A new attention alignment memory."), "later"
    )
    assert run_on_demand(harness.ctx).ok
    call = next(call for call in harness.llm.calls if call.task == "ask")
    assert "A new attention alignment memory." not in call.user
    assert payload["context"]["inputs"] in call.user
    assert (
        check_answers(StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot()))[0].status
        == "current"
    )


def test_note_change_during_model_call_is_billed_once_but_result_not_saved(
    harness, monkeypatch, memory_case
):
    a, _, _, note = memory_case
    question = _queue(harness, a)
    original = type(harness.llm).complete

    def change_during(self, request):
        result = original(self, request)
        if request.task == "ask":
            harness.ctx.store.reader.delete_annotation(note.id, "during-call")
        return result

    monkeypatch.setattr(type(harness.llm), "complete", change_during)
    assert not run_on_demand(harness.ctx).ok
    assert harness.llm_calls("ask") == 1 and qa.answer(harness.ctx, question.id) is None
    assert harness.ctx.store.jobs.get(qa.question_job_key(question.id)).status == "dead"


def test_saved_answer_becomes_stale_after_selected_note_changes(harness, memory_case):
    a, _, _, note = memory_case
    question = _queue(harness, a)
    assert run_on_demand(harness.ctx).ok
    harness.ctx.store.reader.update_annotation(
        note.id, "Changed attention alignment conclusion.", "later"
    )
    result = check_answers(StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot()))[0]
    assert result.status == "stale" and result.reasons
    assert harness.llm_calls("ask") == 1 and qa.answer(harness.ctx, question.id) is not None


def test_annotation_only_anchors_current_paper_author_quote(harness, memory_case):
    a, b, _, _ = memory_case
    harness.llm.overrides["ask"] = {
        "statements": [
            {
                "text": f"The evidence in [[paper:{b}]] is relevant.",
                "citations": [{"paper_id": b, "page": 2, "quote": THERE}],
                "mentioned_paper_ids": [b],
            }
        ]
    }
    question = _queue(harness, a)
    assert run_on_demand(harness.ctx).ok
    web = create_app(harness.ctx, LocalSecurity(48143, launch_token="memory-launch"), None)
    with TestClient(web, base_url="http://127.0.0.1:48143") as client:
        client.get("/launch", params={"token": "memory-launch"})
        csrf = client.get("/api/session").json()["csrf_token"]
        response = client.post(
            f"/api/papers/{a}/qa/{question.id}/annotation", headers={"x-csrf-token": csrf}
        )
    assert response.status_code == 201
    assert response.json()["paper_id"] == a and response.json()["quote"] is None
    assert response.json()["page"] is None and response.json()["qa_question_id"] == question.id


def test_relation_that_exceeds_budget_does_not_leave_orphan_evidence(
    harness, memory_case, monkeypatch
):
    a, _, _, _ = memory_case
    collect = inputs.collect

    def expanded(*args):
        found = collect(*args)
        # 合法关系说明上限；重复本篇选区使整组关系超过消息预算。
        relations = tuple(
            r.model_copy(update={"explanation": "结论。" * 660}) for r in found.relations
        )
        return replace(found, relations=relations)

    monkeypatch.setattr(inputs, "collect", expanded)
    question = qa.queue(
        harness.ctx,
        a,
        "Compare attention alignment experiments " * 20,
        request_id="relation-budget",
    )
    manifest = harness.ctx.store.jobs.get(qa.question_job_key(question.id)).payload["context"]
    assert not manifest["relations"]
    assert all(not source["id"].startswith("relation:") for source in manifest["sources"])
    assert any(
        source["paper_id"] == a and source["kind"] == "author" for source in manifest["sources"]
    )
    assert run_on_demand(harness.ctx).ok and harness.llm_calls("ask") == 1


def test_profile_change_after_source_recheck_is_rejected_under_final_lock(
    harness, memory_case, monkeypatch
):
    a, _, _, _ = memory_case
    question = _queue(harness, a, "profile-final-lock")
    payload = harness.ctx.store.jobs.get(qa.question_job_key(question.id)).payload
    original = memory_run.state_token
    calls = []

    def change_after_check(basis, manifest):
        token = original(basis, manifest)
        if not calls:
            pause_topic(harness.ctx, "alpha-topic")
        calls.append(True)
        return token

    monkeypatch.setattr(memory_run, "state_token", change_after_check)
    with pytest.raises(PermanentError, match="重新提问"):
        qa.AskStage().process(harness.ctx, payload)
    assert len(calls) == 2 and harness.llm_calls("ask") == 1
    assert qa.answer(harness.ctx, question.id) is None


@pytest.mark.parametrize("field", ["paper_id", "question", "selection", "research_profile"])
def test_actual_message_must_match_frozen_question_and_profile(harness, memory_case, field):
    a, _, _, _ = memory_case
    question = _queue(harness, a, "message-mismatch")
    payload = harness.ctx.store.jobs.get(qa.question_job_key(question.id)).payload
    data = json.loads(payload["context"]["inputs"])
    data[field] = "A substituted actual message."
    manifest = {
        **payload["context"],
        "inputs": json.dumps(data, ensure_ascii=False, sort_keys=True),
    }
    with pytest.raises(PermanentError, match="实际问答消息"):
        qa.AskStage().process(harness.ctx, {**payload, "context": manifest})
    assert harness.llm_calls("ask") == 0 and qa.answer(harness.ctx, question.id) is None


@pytest.mark.parametrize("change", ["model", "provider", "missing"])
def test_saved_complete_producer_is_compared_with_current_request_config(
    harness, memory_case, change
):
    a, _, _, _ = memory_case
    question = _queue(harness, a)
    assert run_on_demand(harness.ctx).ok
    routes = dict(harness.ctx.llm.routes)
    if change == "missing":
        del routes["ask"]
    elif change == "model":
        routes["ask"] = replace(routes["ask"], model="new-request-alias")
    else:
        routes["ask"] = replace(routes["ask"], provider_version="new-provider")
    harness.ctx = replace(harness.ctx, llm=replace(harness.ctx.llm, routes=routes))
    result = check_answers(StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot()))[0]
    assert result.status == ("unknown" if change == "missing" else "stale")
    assert qa.answer(harness.ctx, question.id) is not None and harness.llm_calls("ask") == 1


def test_response_model_alias_does_not_change_complete_request_identity(
    harness, memory_case, monkeypatch
):
    a, _, _, _ = memory_case
    original = type(harness.llm).complete

    def alias(self, request):
        response = original(self, request)
        return replace(response, model="resolved-deployment") if request.task == "ask" else response

    monkeypatch.setattr(type(harness.llm), "complete", alias)
    question = _queue(harness, a)
    assert run_on_demand(harness.ctx).ok
    assert qa.answer(harness.ctx, question.id).model == "resolved-deployment"
    result = check_answers(StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot()))[0]
    assert result.status == "current" and not result.unchecked


@pytest.mark.parametrize("change", ["main_membership", "note_only_document"])
def test_final_lock_tracks_only_metadata_required_by_sent_sources(
    harness, memory_case, monkeypatch, change
):
    a, b, _, _ = memory_case
    original = inputs.collect

    def notes_only(*args):
        found = original(*args)
        sources = tuple(
            s
            for s in found.sources
            if not s.id.startswith("relation:") and (s.paper_id == a or s.kind == "note")
        )
        return replace(found, sources=sources, relations=(), backgrounds={})

    monkeypatch.setattr(inputs, "collect", notes_only)
    question = _queue(harness, a, "metadata-scope")
    manifest = harness.ctx.store.jobs.get(qa.question_job_key(question.id)).payload["context"]
    assert any(s["paper_id"] == b for s in manifest["sources"])
    basis = StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot())
    token = memory_state.state_token(basis, manifest)
    if change == "main_membership":
        basis.snapshot = replace(
            basis.snapshot, library_paper_ids=basis.snapshot.library_paper_ids - {a}
        )
    else:
        fulltexts = dict(basis.snapshot.fulltexts)
        fulltexts[b] = replace(fulltexts[b], doc_key="changed-unused-note-document")
        basis.snapshot = replace(basis.snapshot, fulltexts=fulltexts)
    assert memory_state.check(basis, question, manifest) == ((), ())
    assert memory_state.state_token(basis, manifest) == token


def test_final_lock_still_requires_both_used_relation_endpoints_in_library(harness, memory_case):
    a, _, _, _ = memory_case
    question = _queue(harness, a, "relation-library-scope")
    manifest = harness.ctx.store.jobs.get(qa.question_job_key(question.id)).payload["context"]
    assert manifest["relations"]
    basis = StaleInputs(harness.ctx, harness.ctx.store.stale_snapshot())
    token = memory_state.state_token(basis, manifest)
    basis.snapshot = replace(
        basis.snapshot, library_paper_ids=basis.snapshot.library_paper_ids - {a}
    )
    assert memory_state.state_token(basis, manifest) != token
    assert memory_state.check(basis, question, manifest)[0]
