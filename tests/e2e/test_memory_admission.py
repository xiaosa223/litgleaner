"""真实首次入库事务与按需事实前置，不调用外部服务或收费模型。"""

import logging
from dataclasses import replace

import pytest

from litgleaner.application import inbox, memory_admission, reader
from litgleaner.application.memory_admission import MemoryAdmissionStage
from litgleaner.application.stages.discover import resolve_record
from litgleaner.application.stages.extract import ExtractStage
from litgleaner.application.stages.fulltext import AcquireStage, ParseStage
from litgleaner.domain.errors import PermanentError, TransientError, WaitingForWork
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.reading import NewAnnotation
from litgleaner.ports.catalog import CatalogQuery
from tests.e2e.conftest import paper


def _paper(harness, title="New Paper"):
    return resolve_record(
        harness.ctx.store.papers, SourceRecord(**paper(title)), harness.ctx.now_iso()
    )


def _payload(harness, paper_id):
    return harness.ctx.store.jobs.get(f"memory_admit:{paper_id}").payload


def test_first_reading_state_records_one_admission_and_stable_job(harness):
    paper_id = _paper(harness)
    reader.set_reading_state(harness.ctx, paper_id, "reading", {"page": 1})
    first = harness.ctx.store.admissions.get(paper_id)
    reader.set_reading_state(harness.ctx, paper_id, "read", {"page": 2})
    assert harness.ctx.store.admissions.get(paper_id) == first
    job = harness.ctx.store.jobs.get(f"memory_admit:{paper_id}")
    assert job.stage == "memory_admit" and job.payload == {"paper_id": paper_id, "id": first.id}
    assert harness.llm_calls() == 0


def test_first_annotation_records_admission_and_reentry_never_reopens(harness):
    paper_id = _paper(harness)
    note = reader.add_annotation(
        harness.ctx, NewAnnotation(paper_id, "note", None, None, None, "My note")
    )
    first = harness.ctx.store.admissions.get(paper_id)
    harness.ctx.store.jobs.mark_done(f"memory_admit:{paper_id}", harness.ctx.now_iso())
    reader.remove_annotation(harness.ctx, note.id)
    reader.add_annotation(harness.ctx, NewAnnotation(paper_id, "note", None, None, None, "Again"))
    assert harness.ctx.store.admissions.get(paper_id) == first
    assert harness.ctx.store.jobs.get(f"memory_admit:{paper_id}").status == "done"


@pytest.mark.parametrize("operation", ["state", "annotation", "inbox"])
def test_user_mutation_and_admission_roll_back_if_enqueue_fails(harness, monkeypatch, operation):
    paper_id = _paper(harness)

    def fail(*args, **kwargs):
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(harness.ctx.store.jobs, "enqueue", fail)
    with pytest.raises(RuntimeError, match="queue unavailable"):
        if operation == "state":
            reader.set_reading_state(harness.ctx, paper_id, "reading", None)
        elif operation == "annotation":
            reader.add_annotation(
                harness.ctx, NewAnnotation(paper_id, "note", None, None, None, "Note")
            )
        else:
            inbox.add_identifier(harness.ctx, "10.1000/new-paper")
    assert harness.ctx.store.admissions.get(paper_id) is None
    assert harness.ctx.store.reader.get_state(paper_id) is None
    assert harness.ctx.store.reader.annotations(paper_id) == ()
    assert harness.ctx.store.catalog.page(CatalogQuery()).total == 0


def test_identifier_finishes_and_admits_in_one_transaction(harness):
    paper_id = _paper(harness)
    result = inbox.add_identifier(harness.ctx, "10.1000/new-paper")
    assert result.status == "done" and result.paper_id == paper_id
    assert harness.ctx.store.admissions.get(paper_id).id == _payload(harness, paper_id)["id"]
    assert harness.ctx.store.reader.get_state(paper_id).status == "queued"


def test_reading_gets_and_empty_plan_never_admit_or_call_models(harness):
    paper_id = _paper(harness)
    reader.paper_view(harness.ctx, paper_id)
    reader.document_view(harness.ctx, paper_id)
    assert tuple(MemoryAdmissionStage().plan(harness.ctx)) == ()
    assert harness.ctx.store.admissions.get(paper_id) is None
    assert harness.ctx.store.jobs.due("memory_admit", harness.ctx.now_iso()) == ()
    assert harness.llm_calls() == 0


def test_no_relate_route_prepares_only_new_paper_facts(harness, caplog):
    old, new = _paper(harness, "Old Paper"), _paper(harness)
    reader.set_reading_state(harness.ctx, old, "reading", None)
    reader.set_reading_state(harness.ctx, new, "reading", None)
    caplog.set_level(logging.INFO)
    MemoryAdmissionStage().process(harness.ctx, _payload(harness, new))
    assert harness.ctx.store.papers.get_extraction(new) is not None
    assert harness.ctx.store.papers.get_fulltext(old) is None
    assert harness.ctx.store.papers.get_extraction(old) is None
    assert harness.llm_calls("extract") == 1 and harness.llm_calls("relate") == 0
    assert "本次只保存事实" in caplog.text


def test_later_model_route_cannot_charge_old_admission(harness, caplog):
    paper_id = _paper(harness)
    reader.set_reading_state(harness.ctx, paper_id, "reading", None)
    payload = _payload(harness, paper_id)
    harness.edit_yaml(
        "llm.yaml",
        lambda data: data["tasks"].update(
            relate={"provider": "main", "model": "demo-cheap", "prompt": "relate"}
        ),
    )
    harness.rebuild()
    caplog.set_level(logging.INFO)
    MemoryAdmissionStage().process(harness.ctx, payload)
    assert harness.llm_calls() == 0 and "配置已变化" in caplog.text


def test_local_prerequisite_retry_waits_without_processing_old_paper(harness, monkeypatch):
    old, new = _paper(harness, "Old Paper"), _paper(harness)
    reader.set_reading_state(harness.ctx, old, "reading", None)
    reader.set_reading_state(harness.ctx, new, "reading", None)

    def unavailable(_request):
        raise TransientError("temporarily offline")

    monkeypatch.setattr(harness.ctx.fulltext[0], "fetch", unavailable)
    with pytest.raises(WaitingForWork, match="acquire"):
        MemoryAdmissionStage().process(harness.ctx, _payload(harness, new))
    job = harness.ctx.store.jobs.get(f"acquire:{new}")
    assert job.status == "pending" and job.attempts == 1
    assert harness.ctx.store.jobs.get(f"acquire:{old}") is None
    assert harness.llm_calls() == 0


def test_terminal_prerequisite_finishes_with_explicit_reason(harness, monkeypatch, caplog):
    paper_id = _paper(harness, "NoPDF Paper")
    reader.set_reading_state(harness.ctx, paper_id, "reading", None)
    caplog.set_level(logging.INFO)
    MemoryAdmissionStage().process(harness.ctx, _payload(harness, paper_id))
    assert harness.ctx.store.papers.get_fulltext(paper_id).status == "unavailable"
    assert harness.llm_calls() == 0 and "缺少可核实原文" in caplog.text


def test_prepared_queue_uses_only_earlier_admissions(harness, monkeypatch):
    ids = tuple(_paper(harness, title) for title in ("Earlier", "New", "Later"))
    for paper_id in ids:
        reader.set_reading_state(harness.ctx, paper_id, "reading", None)
    event = harness.ctx.store.admissions.get(ids[1])
    monkeypatch.setattr(memory_admission, "configuration", lambda ctx: event.configuration)
    event = replace(event, configuration={**event.configuration, "producer": "ready"})
    original = harness.ctx.store.admissions.get
    monkeypatch.setattr(
        harness.ctx.store.admissions, "get", lambda id_: event if id_ == ids[1] else original(id_)
    )
    monkeypatch.setattr(memory_admission, "configuration", lambda ctx: event.configuration)
    received, prepared = [], object()
    monkeypatch.setattr(
        memory_admission,
        "prepare_input",
        lambda ctx, id_, eligible_ids: received.append((id_, eligible_ids)) or prepared,
    )
    monkeypatch.setattr(
        "litgleaner.application.memory_admission.queue_prepared",
        lambda ctx, id_, input_, scope_id: received.append((id_, input_, scope_id)),
    )
    MemoryAdmissionStage().process(harness.ctx, _payload(harness, ids[1]))
    assert received == [(ids[1], (ids[0],)), (ids[1], prepared, f"memory_admit:{event.id}")]
    assert harness.llm_calls("extract") == 1


def test_removed_note_only_paper_finishes_without_creating_facts(harness, caplog):
    paper_id = _paper(harness)
    note = reader.add_annotation(
        harness.ctx, NewAnnotation(paper_id, "note", None, None, None, "Only note")
    )
    reader.remove_annotation(harness.ctx, note.id)
    caplog.set_level(logging.INFO)
    MemoryAdmissionStage().process(harness.ctx, _payload(harness, paper_id))
    assert harness.ctx.store.papers.get_fulltext(paper_id) is None
    assert harness.llm_calls() == 0 and "已退出文库" in caplog.text


@pytest.mark.parametrize("stage_name", ["acquire", "parse", "extract"])
def test_dead_local_prerequisite_is_terminal_and_never_queues_relate(
    harness, monkeypatch, caplog, stage_name
):
    stages = {"acquire": AcquireStage, "parse": ParseStage, "extract": ExtractStage}
    paper_id = _paper(harness)
    reader.set_reading_state(harness.ctx, paper_id, "reading", None)

    def failed(*args):
        raise PermanentError("prerequisite unavailable")

    monkeypatch.setattr(stages[stage_name], "process", failed)
    caplog.set_level(logging.INFO)
    MemoryAdmissionStage().process(harness.ctx, _payload(harness, paper_id))
    (job,) = harness.ctx.store.jobs.list_by_status("dead")
    assert job.stage == stage_name
    assert "前置已终止" in caplog.text and "缺少可核实原文" in caplog.text
    assert harness.ctx.store.jobs.due("relate", harness.ctx.now_iso()) == ()
    assert harness.llm_calls() == 0
