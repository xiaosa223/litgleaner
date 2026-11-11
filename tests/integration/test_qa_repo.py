"""问答事实存储、不可覆盖与全文检索；引用内容均为人工测试种子。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from ulid import ULID

from litgleaner.domain.paper import Paper
from litgleaner.domain.qa import Answer, Citation, Question, Statement
from litgleaner.domain.reading import NewAnnotation
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import qa_message
from litgleaner.infra.db.store import SqlStore


@pytest.fixture
def store(tmp_path: Path):
    url = f"sqlite:///{tmp_path / 'qa.db'}"
    upgrade_database(url)
    store = SqlStore(url)
    for id_ in ("P1", "P2"):
        store.papers.insert(Paper(id_, f"Paper {id_}", id_, None, ("A",), None, None, None, "t"))
    yield store
    store.dispose()


def question(**changes):
    return replace(
        Question(str(ULID()), "P1", str(ULID()), "我的天文问题 quasar", None, 2, "zh-CN", "t1"),
        **changes,
    )


def answer(item: Question, **changes):
    return replace(
        Answer(
            str(ULID()),
            item.paper_id,
            item.id,
            (
                Statement("A test inference.", (), True),
                Statement(
                    "The spectroscopy answer.",
                    (
                        Citation(item.paper_id, 4, "Synthetic spectral evidence."),
                        Citation(item.paper_id, 1, "Another synthetic quote."),
                    ),
                    False,
                ),
            ),
            "test-model",
            "producer-v1",
            "input-hash",
            "t2",
        ),
        **changes,
    )


def test_question_roundtrip_preserves_user_text_selection_language_and_zero_page(store):
    item = question(
        text="  user's 问题\n'); DROP TABLE paper; --  ",
        selection="原文选区\ndo not rewrite",
        page=0,
        reading_language="en",
    )
    assert store.qa.add_question(item)
    assert store.qa.question(item.id) == item
    assert store.qa.by_request(item.paper_id, item.request_id) == item
    assert store.qa.questions("P2") == () and store.papers.get("P1") is not None
    row = store._db.fetch(select(qa_message))[0]
    assert (row.text, row.page, row.request_id, row.paper_id) == (
        item.text,
        0,
        item.request_id,
        item.paper_id,
    )
    assert json.loads(row.data) == {"selection": item.selection, "reading_language": "en"}


def test_missing_question_request_and_answer_return_none(store):
    assert store.qa.question("missing") is None
    assert store.qa.by_request("P1", "missing") is None
    assert store.qa.answer("missing") is None


def test_answer_roundtrip_restores_nested_dataclasses_and_tuple_order(store):
    item = question()
    store.qa.add_question(item)
    reply = answer(item)
    assert store.qa.add_answer(reply)
    loaded = store.qa.answer(item.id)
    assert loaded == reply and isinstance(loaded, Answer)
    assert isinstance(loaded.statements, tuple) and isinstance(loaded.statements[1], Statement)
    assert isinstance(loaded.statements[1].citations, tuple)
    assert isinstance(loaded.statements[1].citations[0], Citation)
    assert store.qa.question(reply.id) is None
    row = store._db.fetch(select(qa_message).where(qa_message.c.id == reply.id))[0]
    assert row.parent_id == item.id and row.request_id is None and row.page == 4
    assert row.text == "A test inference.\nThe spectroscopy answer."
    assert set(json.loads(row.data)) == {"statements", "model", "producer_version", "input_hash"}


def test_question_request_is_idempotent_and_never_replaces_original_words(store):
    first = question()
    assert store.qa.add_question(first) and not store.qa.add_question(first)
    changed = replace(first, id=str(ULID()), text="changed text", page=9, created_at="t9")
    assert not store.qa.add_question(changed)
    assert store.qa.by_request("P1", first.request_id) == first
    assert store.qa.question(changed.id) is None
    other = replace(changed, paper_id="P2")
    assert store.qa.add_question(other) and store.qa.by_request("P2", first.request_id) == other


def test_answer_is_idempotent_and_keeps_first_model_result(store):
    item = question()
    store.qa.add_question(item)
    first = answer(item)
    assert store.qa.add_answer(first) and not store.qa.add_answer(first)
    changed = replace(first, id=str(ULID()), statements=(Statement("replacement", (), True),))
    assert not store.qa.add_answer(changed)
    assert store.qa.answer(item.id) == first
    hits = [hit for hit in store.search.find("spectroscopy", 20) if hit.kind == "qa"]
    assert [hit.id for hit in hits] == [first.id]
    assert store.search.find("replacement", 20) == ()


def test_question_and_answer_primary_key_collisions_are_not_silently_ignored(store):
    first, second = question(), question()
    store.qa.add_question(first)
    store.qa.add_question(second)
    with pytest.raises(IntegrityError, match="qa_identity_conflict"):
        store.qa.add_question(replace(second, id=first.id, request_id="another-request"))
    with pytest.raises(IntegrityError, match="qa_identity_conflict"):
        store.qa.add_answer(answer(second, id=first.id))
    assert store.qa.question(first.id) == first and store.qa.answer(second.id) is None


def test_id_collision_cannot_hide_behind_question_or_answer_idempotency(store):
    first, second = question(), question()
    store.qa.add_question(first)
    store.qa.add_question(second)
    first_reply, second_reply = answer(first), answer(second)
    store.qa.add_answer(first_reply)
    store.qa.add_answer(second_reply)
    with pytest.raises(IntegrityError, match="qa_identity_conflict"):
        store.qa.add_question(replace(second, request_id=first.request_id))
    with pytest.raises(IntegrityError, match="qa_identity_conflict"):
        store.qa.add_answer(replace(second_reply, question_id=first.id))
    assert store.qa.question(first.id) == first and store.qa.question(second.id) == second
    assert store.qa.answer(first.id) == first_reply and store.qa.answer(second.id) == second_reply


@pytest.mark.parametrize(
    "invalid_parent", ["missing", "other_paper", "answer", "answered_other_paper"]
)
def test_answer_parent_must_be_an_existing_question_in_same_paper(store, invalid_parent):
    item = question()
    store.qa.add_question(item)
    reply = answer(item)
    if invalid_parent in {"answer", "answered_other_paper"}:
        store.qa.add_answer(reply)
    invalid = answer(item)
    if invalid_parent == "missing":
        invalid = replace(invalid, question_id=str(ULID()))
    elif invalid_parent == "answer":
        invalid = replace(invalid, question_id=reply.id)
    else:
        invalid = replace(invalid, paper_id="P2")
    with pytest.raises(IntegrityError, match="qa_parent_invalid"):
        store.qa.add_answer(invalid)
    assert store.qa.questions("P2") == ()
    assert store.qa.answer(item.id) == (
        reply if invalid_parent in {"answer", "answered_other_paper"} else None
    )


def test_missing_paper_and_invalid_candidate_are_not_hidden_by_idempotency(store):
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        store.qa.add_question(question(paper_id="missing"))
    item = question()
    store.qa.add_question(item)
    for invalid in (replace(item, page=-1), replace(item, text="")):
        with pytest.raises(IntegrityError, match="CHECK"):
            store.qa.add_question(invalid)
    store.qa.add_answer(answer(item))
    with pytest.raises(IntegrityError, match="CHECK"):
        store.qa.add_answer(answer(item, statements=()))


def test_latest_questions_are_returned_in_stable_chronological_order(store):
    items = [question(created_at=f"2026-11-{1 + index // 3:02d}") for index in range(55)]
    other = question(paper_id="P2", created_at="2099")
    for item in reversed([*items, other]):
        store.qa.add_question(item)
    store.qa.add_answer(answer(items[-1]))
    ordered = sorted(items, key=lambda item: (item.created_at, item.id))
    assert store.qa.questions("P1") == tuple(ordered[-50:])
    assert store.qa.questions("P1", 3) == tuple(ordered[-3:])
    assert store.qa.questions("P1", 100) == tuple(ordered)
    assert store.qa.questions("P1", 0) == store.qa.questions("P1", -1) == ()


def test_question_and_answer_search_hits_keep_authorship_id_and_first_citation_page(store):
    item = question()
    reply = answer(item)
    store.qa.add_question(item)
    store.qa.add_answer(reply)
    for query in ("天文", "天文问题", "quasar"):
        (hit,) = store.search.find(query, 20)
        assert (hit.kind, hit.id, hit.paper_id, hit.page, hit.authorship) == (
            "qa",
            item.id,
            "P1",
            2,
            "user",
        )
        assert hit.title == "Paper P1" and hit.text == item.text
    (hit,) = store.search.find("spectroscopy", 20)
    assert (hit.kind, hit.id, hit.page, hit.authorship) == ("qa", reply.id, 4, "model")
    assert store.search.find("Synthetic spectral evidence", 20) == ()
    assert len({hit.id for hit in store.search.find("test", 20)}) == 1


def test_answer_without_citations_is_searchable_with_no_invented_page(store):
    item = question(page=None, selection=None)
    store.qa.add_question(item)
    reply = answer(item, statements=(Statement("Uncited hypothesis", (), True),))
    store.qa.add_answer(reply)
    (hit,) = store.search.find("hypothesis", 20)
    assert hit.page is None and hit.authorship == "model"
    assert store.qa.answer(item.id) == reply


def test_matching_question_and_answer_are_distinct_hits_in_same_search(store):
    item = question(text="Shared quasar prompt")
    reply = answer(item, statements=(Statement("Shared quasar model answer", (), True),))
    store.qa.add_question(item)
    store.qa.add_answer(reply)
    hits = store.search.find("shared quasar", 20)
    assert {hit.id: hit.authorship for hit in hits} == {item.id: "user", reply.id: "model"}
    assert len(hits) == 2 and all(hit.kind == "qa" and hit.paper_id == "P1" for hit in hits)


def test_linked_annotation_keeps_authorship_and_user_edits_leave_source_qa_unchanged(store):
    item = question(text="User pulsar question", selection="Original stellar quote")
    reply = answer(item)
    store.qa.add_question(item)
    store.qa.add_answer(reply)
    note = store.reader.add_annotation(
        NewAnnotation(
            "P1",
            "note",
            item.page,
            None,
            item.selection,
            item.text,
            qa_question_id=item.id,
        ),
        "t3",
    )
    assert note.qa_question_id == item.id
    assert store.reader.get_annotation(note.id) == note
    assert store.reader.annotations("P1") == (note,)
    assert store.reader.update_annotation(note.id, "My edited nebula note", "t4")
    changed = store.reader.get_annotation(note.id)
    assert changed.body == "My edited nebula note" and changed.qa_question_id == item.id
    assert store.qa.question(changed.qa_question_id) == item and store.qa.answer(item.id) == reply
    model_hits = store.search.find("spectroscopy", 20)
    assert [(hit.id, hit.kind, hit.authorship) for hit in model_hits] == [(reply.id, "qa", "model")]
    assert store.search.find("nebula", 20)[0].authorship == "user"
    quote = store.search.find("stellar quote", 20)[0]
    assert quote.kind == "highlight" and quote.authorship == "author"
    assert store.reader.delete_annotation(note.id, "t5")
    assert store.qa.question(item.id) == item and store.qa.answer(item.id) == reply
    assert store.search.find("nebula", 20) == ()


def test_existing_annotation_callers_keep_optional_qa_link_empty(store):
    item = NewAnnotation("P1", "note", None, None, None, "Legacy personal note")
    assert item.qa_question_id is None
    stored = store.reader.add_annotation(item, "t1")
    assert stored.qa_question_id is None and store.reader.get_annotation(stored.id) == stored


def test_atomic_rollback_keeps_qa_and_fts_in_sync(store):
    item = question()
    with pytest.raises(RuntimeError), store.atomic():
        store.qa.add_question(item)
        store.qa.add_answer(answer(item))
        assert store.search.find("quasar", 20)
        raise RuntimeError("test rollback")
    assert store.qa.question(item.id) is None and store.qa.answer(item.id) is None
    assert store.search.find("quasar", 20) == store.search.find("spectroscopy", 20) == ()
