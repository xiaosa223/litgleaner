"""问答记忆保留规范来源、旧 JSON 兼容和批量快照的只读边界。"""

import json
import sqlite3
from dataclasses import asdict, replace

import pytest
from sqlalchemy import event, insert, select, text
from tests.integration.test_export_repo import paper
from tests.integration.test_qa_repo import answer, question

from litgleaner.domain.document import Block
from litgleaner.domain.qa import Citation, NoteCitation, Statement
from litgleaner.domain.reading import NewAnnotation
from litgleaner.domain.search_text import normalize_search_text
from litgleaner.infra.db import stale_repo
from litgleaner.infra.db.migrate import upgrade_database
from litgleaner.infra.db.schema import qa_message
from litgleaner.infra.db.store import SqlStore
from litgleaner.ports.store import FullTextState


@pytest.fixture
def store(tmp_path):
    url = f"sqlite:///{tmp_path / 'qa-memory.db'}"
    upgrade_database(url)
    result = SqlStore(url)
    for key in ("P1", "P2", "outside"):
        result.papers.insert(paper(key))
    yield result
    result.dispose()


def note(store, *, paper_id="P1", body="User quasar evidence", kind="note", page=None):
    return store.reader.add_annotation(
        NewAnnotation(paper_id, kind, page, None, "Author quasar quote", body), "t1"
    )


def document(store, paper_id="P1", key="current", content="Author quasar evidence"):
    store.papers.save_fulltext(FullTextState(paper_id, "parsed", doc_key=key), "t")
    store.search.replace_document(paper_id, key, (Block("text", content, 0),))


def fts(store, **changes):
    values = {
        "kind": "note",
        "id": "forged",
        "paper_id": "P1",
        "page": None,
        "authorship": "user",
        "source_version": "",
        "title": "",
        "content": "quasar quasar quasar quasar",
    }
    values.update(changes)
    values.update(
        search_title=normalize_search_text(values["title"]),
        search_text=normalize_search_text(values["content"]),
    )
    store._db.write(
        text(
            "INSERT INTO memory_fts"
            "(kind,id,paper_id,page,authorship,source_version,title,text,search_title,search_text) "
            "VALUES(:kind,:id,:paper_id,:page,:authorship,:source_version,:title,:content,"
            ":search_title,:search_text)"
        ).bindparams(**values)
    )


def test_extended_answer_roundtrips_context_note_sources_and_declared_papers(store):
    item = question()
    store.qa.add_question(item)
    statement = Statement(
        "Compared evidence",
        (Citation("P2", 9, "Author evidence from another paper"),),
        False,
        (
            NoteCitation("P2", "annotation-1", "User evidence from another paper"),
            NoteCitation("P1", "question-1", "A historical user question", "question"),
        ),
        ("P1", "P2"),
    )
    reply = answer(
        item,
        statements=(statement,),
        context={"contract": "test", "sources": [{"page": None}]},
        request_producer_version="request-producer",
    )
    assert store.qa.add_answer(reply)
    assert store.qa.answer(item.id) == reply
    row = store._db.fetch(select(qa_message).where(qa_message.c.id == reply.id))[0]
    assert row.page is None
    assert json.loads(row.data)["request_producer_version"] == "request-producer"
    assert store.stale_snapshot().answers == (reply,)


def test_answer_search_page_only_uses_current_paper_author_citation(store):
    item = question()
    store.qa.add_question(item)
    statement = Statement(
        "quasar answer",
        (Citation("P2", 8, "Other original evidence"), Citation("P1", 2, "Own original evidence")),
        False,
    )
    reply = answer(item, statements=(statement,))
    store.qa.add_answer(reply)
    row = store._db.fetch(select(qa_message).where(qa_message.c.id == reply.id))[0]
    assert row.page == 2


def test_legacy_answer_json_omits_new_fields_and_restores_defaults(store):
    item = question()
    store.qa.add_question(item)
    reply = answer(item)
    legacy = {
        "statements": [
            {
                "text": s.text,
                "citations": [asdict(c) for c in s.citations],
                "inference": s.inference,
            }
            for s in reply.statements
        ],
        "model": reply.model,
        "producer_version": reply.producer_version,
        "input_hash": reply.input_hash,
    }
    store._db.write(
        insert(qa_message).values(
            id=reply.id,
            paper_id=reply.paper_id,
            role="answer",
            parent_id=reply.question_id,
            request_id=None,
            text="Legacy answer text",
            page=None,
            data=json.dumps(legacy),
            created_at=reply.created_at,
        )
    )
    loaded = store.qa.answer(item.id)
    assert loaded == reply and loaded.context == {} and loaded.request_producer_version is None
    data = json.loads(
        store._db.fetch(select(qa_message).where(qa_message.c.id == reply.id))[0].data
    )
    assert set(data) == {"statements", "model", "producer_version", "input_hash"}


def test_qa_hits_keep_complete_text_authorship_and_raw_source_identity(store):
    original = "Opening. " + "quasar evidence " * 60
    document(store, content=original)
    annotation = note(store, body=original)
    item = question(text=original, page=None)
    store.qa.add_question(item)
    store.qa.add_answer(answer(item, statements=(Statement(original, (), True),)))
    hits = store.search.qa_hits("quasar", ("P1",))
    assert len(hits) == 3 and {hit.authorship for hit in hits} == {"author", "user"}
    assert all(hit.text == original and len(hit.text) > 320 for hit in hits)
    assert {hit.id for hit in hits} == {"0", annotation.id + ":body", item.id}
    author = next(hit for hit in hits if hit.kind == "fulltext")
    assert author.page == 0 and author.source_version == "current" and author.rank < 0
    assert next(hit for hit in hits if hit.id == annotation.id + ":body").page is None


def test_scope_and_canonical_filters_run_before_limit(store):
    note(store, paper_id="outside", body="quasar " * 100)
    current = note(store, body="quasar")
    fts(store)
    fts(store, id=current.id + ":body", content="quasar forged content " * 100)
    fts(store, id=current.id + ":body", authorship="model")
    document(store, key="old", content="quasar " * 100)
    store.papers.save_fulltext(FullTextState("P1", "parsed", doc_key="new"), "t2")
    (hit,) = store.search.qa_hits("quasar", ("P1",), limit=1)
    assert hit.id == current.id + ":body" and hit.text == "quasar"


def test_soft_deleted_and_changed_notes_do_not_survive_stale_fts_rows(store):
    removed = note(store)
    changed = note(store)
    store.reader.delete_annotation(removed.id, "t2")
    store.reader.update_annotation(changed.id, "A different current note", "t2")
    fts(store, id=removed.id + ":body", content=removed.body)
    fts(store, id=changed.id + ":body", content=changed.body)
    assert store.search.qa_hits("quasar", ("P1",)) == ()


def test_paper_metadata_extraction_model_qa_and_author_highlights_are_not_personal_sources(store):
    item = question(text="User ordinary question")
    store.qa.add_question(item)
    reply = answer(item, statements=(Statement("quasar model answer", (), True),))
    store.qa.add_answer(reply)
    fts(store, kind="qa", id=reply.id, authorship="user", content="quasar model answer")
    note(store, body="Unrelated user note")
    store.papers.insert_extraction("P1", "input", "producer", {"summary": "quasar summary"}, "t")
    fts(store, kind="paper", id="P1", authorship="author")
    assert store.search.qa_hits("quasar", ("P1",)) == ()


@pytest.mark.parametrize("query", ["rare quasar", "rare OR quasar", '"rare" (quasar)', "星 quasar"])
def test_queries_are_literal_or_recall_including_short_words(store, query):
    one = note(store, body="quasar author reading")
    two = note(store, body="我的星阅读记录")
    hits = store.search.qa_hits(query, ("P1",))
    assert one.id + ":body" in {hit.id for hit in hits}
    if "星" in query:
        assert two.id + ":body" in {hit.id for hit in hits}


def test_short_only_query_uses_literal_match_without_fts_operator_or_wildcard(store):
    current = note(store, body="我的星阅读记录")
    (hit,) = store.search.qa_hits("星", ("P1",))
    assert hit.id == current.id + ":body" and hit.rank == 0
    assert store.search.qa_hits("_", ("P1",)) == ()


def test_long_paper_id_pool_is_one_parameter_not_sqlite_variable_overflow(store):
    current = note(store)
    with store._db._engine.connect() as connection:
        connection.connection.driver_connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    ids = (*(f"missing-{n}" for n in range(1100)), "P1")
    (hit,) = store.search.qa_hits("quasar", ids)
    assert hit.id == current.id + ":body"


def test_empty_scope_empty_query_nonpositive_limit_and_missing_fts_are_empty(store):
    note(store)
    for query, ids, limit in (("", ("P1",), 64), ("quasar", (), 64), ("quasar", ("P1",), 0)):
        assert store.search.qa_hits(query, ids, limit) == ()
    store._db.write(text("DROP TABLE memory_fts"))
    assert store.search.qa_hits("quasar", ("P1",)) == ()


def test_qa_limit_is_capped_at_sixty_four_after_valid_source_filters(store):
    with store.atomic():
        for n in range(70):
            note(store, body=f"quasar evidence {n}")
    assert len(store.search.qa_hits("quasar", ("P1",), 1000)) == 64


def test_stale_snapshot_has_eighteen_reads_canonical_notes_and_no_writes(store, monkeypatch):
    first = note(store)
    second = note(store, kind="highlight", page=0)
    deleted = note(store)
    store.reader.delete_annotation(deleted.id, "t2")
    store.reader.update_annotation(first.id, "Updated user note", "t3")
    statements, connections = [], set()
    convert = stale_repo._snapshot

    def captured(conn, _cursor, sql, _params, _context, _many):
        statements.append(sql)
        connections.add(id(conn))
        assert sql.startswith(("SELECT", "BEGIN"))

    def convert_outside(rows):
        assert store._db._active is None
        return convert(rows)

    def forbidden(*_args, **_kwargs):
        pytest.fail("snapshot must not write")

    monkeypatch.setattr(store._db, "write", forbidden)
    monkeypatch.setattr(stale_repo, "_snapshot", convert_outside)
    event.listen(store._db._engine, "before_cursor_execute", captured)
    try:
        loaded = store.stale_snapshot()
    finally:
        event.remove(store._db._engine, "before_cursor_execute", captured)
    assert len([sql for sql in statements if sql.startswith("SELECT")]) == 18
    assert len(connections) == 1
    assert loaded.annotations == tuple(
        sorted(
            (replace(first, body="Updated user note", updated_at="t3"), second),
            key=lambda a: (a.paper_id, a.id),
        )
    )
