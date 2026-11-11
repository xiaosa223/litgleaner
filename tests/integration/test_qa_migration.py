"""0010 问答迁移：约束、旧 FTS 保留、触发器及升降级一致性。"""

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.exc import IntegrityError
from ulid import ULID

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.infra.db.schema import annotation, metadata, qa_message
from litgleaner.infra.db.schema_rules import include_schema_object
from litgleaner.infra.db.search_repo import SqlSearchRepo


def _legacy(path: Path) -> str:
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0009")
    with sqlite3.connect(path) as conn:
        for id_ in ("P1", "P2"):
            conn.execute(
                "INSERT INTO paper(id,title,title_key,authors,first_seen_at) "
                "VALUES (?,?,?,'[]','t')",
                (id_, f"Legacy Astronomy {id_}", id_),
            )
        conn.execute(
            "INSERT INTO annotation(id,paper_id,kind,page,body,created_at,updated_at) "
            "VALUES ('N1','P1','note',0,'用户旧笔记 legacy-memory','t','t')"
        )
        conn.execute(
            "INSERT INTO memory_fts(kind,id,paper_id,page,authorship,source_version,title,text) "
            "VALUES ('fulltext','0','P1',1,'author','doc1','','Preserved full text')"
        )
    return url


def _question(**changes):
    return {
        "id": str(ULID()),
        "paper_id": "P1",
        "role": "question",
        "parent_id": None,
        "request_id": str(ULID()),
        "text": "Question pulsar",
        "page": 0,
        "data": '{"selection":null,"reading_language":"zh-CN"}',
        "created_at": "t1",
        **changes,
    }


@pytest.fixture
def db(tmp_path: Path):
    url = _legacy(tmp_path / "qa.db")
    upgrade_database(url)
    database = Database(url)
    yield database
    database.dispose()


def test_upgrade_preserves_existing_fts_rows_tokenizer_and_trigger_definitions(tmp_path):
    path = tmp_path / "upgrade.db"
    url = _legacy(path)
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT * FROM memory_fts ORDER BY rowid").fetchall()
        old_annotations = conn.execute("SELECT * FROM annotation ORDER BY id").fetchall()
        original = conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE name='memory_fts' OR type='trigger' "
            "ORDER BY name"
        ).fetchall()
    upgrade_database(url)
    upgrade_database(url)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM memory_fts ORDER BY rowid").fetchall() == before
        upgraded = conn.execute("SELECT * FROM annotation ORDER BY id").fetchall()
        assert [row[:-1] for row in upgraded] == old_annotations
        assert all(row[-1] is None for row in upgraded)
        links = conn.execute("PRAGMA foreign_key_list(annotation)").fetchall()
        assert any(row[2:5] == ("qa_message", "qa_question_id", "id") for row in links)
        current = dict(
            conn.execute(
                "SELECT name,sql FROM sqlite_master WHERE name='memory_fts' OR type='trigger'"
            ).fetchall()
        )
        assert all(current[name] == sql for name, sql in original)
        assert "trigram" in current["memory_fts"]
        assert sum(name.startswith("search_") for name in current) == 12
        assert conn.execute("SELECT count(*) FROM qa_message").fetchone()[0] == 0
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
    database = Database(url)
    try:
        for query in ("Astronomy", "旧笔记", "笔记"):
            assert SqlSearchRepo(database).find(query, 20)
    finally:
        database.dispose()


def test_qa_migration_matches_schema_with_named_constraints_and_order_index(tmp_path):
    url = _legacy(tmp_path / "schema.db")
    upgrade_database(url)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"include_object": include_schema_object}
            )
            assert compare_metadata(context, metadata) == []
            sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='qa_message'"
            ).scalar_one()
            for constraint in ("ck_qa_role_fields", "ck_qa_text", "ck_qa_page", "ck_qa_data"):
                assert constraint in sql
            index = connection.exec_driver_sql("PRAGMA index_info(ix_qa_paper_created)").all()
            assert [row[2] for row in index] == ["paper_id", "created_at", "id"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "system"},
        {"request_id": None},
        {"request_id": "   "},
        {"parent_id": "invalid"},
        {"text": ""},
        {"text": "   "},
        {"page": -1},
        {"page": 1.5},
        {"data": "not JSON"},
    ],
)
def test_database_rejects_invalid_question_shape_even_without_repo(db, changes):
    with pytest.raises(IntegrityError, match="CHECK"):
        db.write(insert(qa_message).values(**_question(**changes)))
    assert db.fetch(select(qa_message)) == []


def test_database_rejects_answer_request_id_and_unknown_paper(db):
    item = _question()
    db.write(insert(qa_message).values(**item))
    with pytest.raises(IntegrityError, match="CHECK"):
        db.write(insert(qa_message).values(**_question(role="answer", parent_id=item["id"])))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.write(insert(qa_message).values(**_question(paper_id="absent")))


def test_parent_trigger_rejects_self_reference_and_cross_paper_raw_insert(db):
    item = _question()
    db.write(insert(qa_message).values(**item))
    for invalid in (
        _question(role="answer", parent_id=item["id"], request_id=None, paper_id="P2"),
        _question(id="self", role="answer", parent_id="self", request_id=None),
    ):
        with pytest.raises(IntegrityError, match="qa_parent_invalid"):
            db.write(insert(qa_message).values(**invalid))
    assert len(db.fetch(select(qa_message))) == 1


def test_database_prevents_updates_without_changing_original_words_or_index(db):
    item = _question()
    db.write(insert(qa_message).values(**item))
    with pytest.raises(IntegrityError, match="qa_message_immutable"):
        db.write(update(qa_message).where(qa_message.c.id == item["id"]).values(text="replaced"))
    assert db.fetch(select(qa_message))[0].text == item["text"]
    assert SqlSearchRepo(db).find("pulsar", 20)[0].id == item["id"]
    assert SqlSearchRepo(db).find("replaced", 20) == ()


def test_qa_trigger_projection_uses_columns_and_deletion_removes_only_qa_hit(db):
    item = _question()
    db.write(insert(qa_message).values(**item))
    reply = _question(
        role="answer",
        parent_id=item["id"],
        request_id=None,
        text="Answer neutron",
        page=3,
        data="{}",
    )
    db.write(insert(qa_message).values(**reply))
    (hit,) = SqlSearchRepo(db).find("neutron", 20)
    assert (hit.id, hit.kind, hit.page, hit.authorship) == (reply["id"], "qa", 3, "model")
    db.write(qa_message.delete().where(qa_message.c.id == reply["id"]))
    assert SqlSearchRepo(db).find("neutron", 20) == ()
    assert SqlSearchRepo(db).find("pulsar", 20)[0].authorship == "user"
    assert SqlSearchRepo(db).find("legacy-memory", 20)[0].kind == "note"


@pytest.mark.parametrize("operation", ["insert", "update"])
@pytest.mark.parametrize("invalid", ["missing", "other_question", "answer", "other_paper"])
def test_annotation_link_rejects_wrong_parent_on_insert_and_update(db, operation, invalid):
    item, other = _question(), _question(paper_id="P2")
    reply = _question(role="answer", parent_id=item["id"], request_id=None)
    for row in (item, other, reply):
        db.write(insert(qa_message).values(**row))
    initial = {
        "id": "linked",
        "paper_id": "P1",
        "kind": "note",
        "body": "User original",
        "qa_question_id": item["id"],
        "created_at": "t",
        "updated_at": "t",
    }
    targets = {"missing": "absent", "other_question": other["id"], "answer": reply["id"]}
    change = (
        {"paper_id": "P2"} if invalid == "other_paper" else {"qa_question_id": targets[invalid]}
    )
    if operation == "update":
        db.write(insert(annotation).values(**initial))
        statement = update(annotation).where(annotation.c.id == "linked").values(**change)
    else:
        statement = insert(annotation).values(**{**initial, **change})
    with pytest.raises(IntegrityError, match="qa_annotation_question_invalid"):
        db.write(statement)
    rows = db.fetch(select(annotation).where(annotation.c.id == "linked"))
    assert len(rows) == (1 if operation == "update" else 0)
    if rows:
        assert rows[0].paper_id == "P1" and rows[0].qa_question_id == item["id"]


def test_annotation_question_reference_is_a_real_foreign_key(db):
    item = _question()
    db.write(insert(qa_message).values(**item))
    db.write(
        insert(annotation).values(
            id="linked",
            paper_id="P1",
            kind="note",
            body="User original",
            qa_question_id=item["id"],
            created_at="t",
            updated_at="t",
        )
    )
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.write(qa_message.delete().where(qa_message.c.id == item["id"]))
    assert db.fetch(select(qa_message))[0].id == item["id"]


def _add_linked_question_answer_and_annotation(url):
    database = Database(url)
    item = _question()
    database.write(insert(qa_message).values(**item))
    database.write(
        insert(annotation).values(
            id="linked",
            paper_id="P1",
            kind="note",
            body=item["text"],
            quote="Original authors quote",
            qa_question_id=item["id"],
            created_at="t",
            updated_at="t",
        )
    )
    database.write(
        insert(qa_message).values(
            **_question(
                role="answer",
                parent_id=item["id"],
                request_id=None,
                text="Answer neutron",
            )
        )
    )
    database.dispose()


def test_downgrade_removes_only_qa_and_reupgrade_keeps_prior_memory(tmp_path):
    path = tmp_path / "roundtrip.db"
    url = _legacy(path)
    upgrade_database(url)
    _add_linked_question_answer_and_annotation(url)
    with sqlite3.connect(path) as conn:
        old_annotations = [row[:-1] for row in conn.execute("SELECT * FROM annotation ORDER BY id")]
        old_rows = conn.execute(
            "SELECT * FROM memory_fts WHERE kind!='qa' ORDER BY rowid"
        ).fetchall()
    command.downgrade(alembic_config(url), "0009")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM memory_fts ORDER BY rowid").fetchall() == old_rows
        assert conn.execute("SELECT * FROM annotation ORDER BY id").fetchall() == old_annotations
        assert all(
            row[2] != "qa_message" for row in conn.execute("PRAGMA foreign_key_list(annotation)")
        )
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='qa_message' OR name LIKE 'qa_%'"
            ).fetchall()
            == []
        )
        assert conn.execute("SELECT count(*) FROM paper").fetchone()[0] == 2
    upgrade_database(url)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT * FROM memory_fts ORDER BY rowid").fetchall() == old_rows
        assert conn.execute("SELECT count(*) FROM qa_message").fetchone()[0] == 0
        assert all(row[0] is None for row in conn.execute("SELECT qa_question_id FROM annotation"))
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
