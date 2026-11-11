"""0011 概念事实迁移：约束、增量 FTS、旧问答批注及升降级保留。"""

import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, insert, text
from sqlalchemy.exc import IntegrityError

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.infra.db.schema import concept, concept_alias, concept_occurrence, metadata
from litgleaner.infra.db.schema_rules import include_schema_object
from litgleaner.infra.db.store import SqlStore


def _legacy(path: Path) -> str:
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0010")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO paper(id,title,title_key,authors,first_seen_at) "
            "VALUES ('P1','Legacy Astronomy','legacy','[]','t')"
        )
        conn.execute(
            "INSERT INTO qa_message(id,paper_id,role,request_id,text,page,data,created_at) "
            "VALUES ('Q1','P1','question','R1','Legacy user question',3,?,'t')",
            (json.dumps({"selection": None, "reading_language": "en"}),),
        )
        conn.execute(
            "INSERT INTO qa_message(id,paper_id,role,parent_id,text,page,data,created_at) "
            "VALUES ('A1','P1','answer','Q1','Legacy model answer',3,?,'t')",
            (
                json.dumps(
                    {"statements": [], "model": "m", "producer_version": "v", "input_hash": "h"}
                ),
            ),
        )
        conn.execute(
            "INSERT INTO annotation(id,paper_id,kind,page,body,quote,qa_question_id,"
            "created_at,updated_at) "
            "VALUES ('N1','P1','note',3,'Legacy user question','An authors quotation','Q1','t','t')"
        )
    return url


def _snapshot(path: Path):
    with sqlite3.connect(path) as conn:
        rows = {
            name: conn.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()
            for name in ("paper", "qa_message", "annotation", "memory_fts")
        }
        triggers = dict(
            conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
        )
        return rows, triggers


@pytest.fixture
def db(tmp_path):
    url = _legacy(tmp_path / "knowledge.db")
    upgrade_database(url)
    database = Database(url)
    database.write(insert(concept).values(id="C1", name="Known Concept", created_at="t"))
    yield database
    database.dispose()


def test_upgrade_preserves_all_old_rows_triggers_and_matches_schema(tmp_path):
    path = tmp_path / "upgrade.db"
    url = _legacy(path)
    before, triggers = _snapshot(path)
    upgrade_database(url)
    upgrade_database(url)
    after, current = _snapshot(path)
    assert before == after and all(current[name] == sql for name, sql in triggers.items())
    assert sum(name.startswith("search_") for name in current) == 12
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"include_object": include_schema_object}
            )
            assert compare_metadata(context, metadata) == []
            sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE name='memory_fts'"
            ).scalar_one()
            assert "trigram" in sql
    finally:
        engine.dispose()


def _raw_occurrence(**changes):
    return {
        "concept_id": "C1",
        "paper_id": "P1",
        "role": "core",
        "definition": None,
        "page": 0,
        "bbox": None,
        "verified": 0,
        "input_hash": "h",
        "producer_version": "v",
        "created_at": "t",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"role": "unknown"},
        {"page": -1},
        {"page": None},
        {"page": 1.5},
        {"verified": 2},
        {"verified": -1},
        {"verified": None},
        {"verified": 1, "definition": None},
        {"verified": 1, "definition": "too short"},
        {"verified": 1, "definition": "             "},
        {"verified": 0, "definition": "Raw unverified text must stay outside facts"},
        {"bbox": "broken JSON"},
    ],
)
def test_raw_database_rejects_invalid_occurrence_shape(db, changes):
    statement = text(
        "INSERT INTO concept_occurrence(concept_id,paper_id,role,definition,page,bbox,verified,"
        "input_hash,producer_version,created_at) VALUES (:concept_id,:paper_id,:role,:definition,"
        ":page,:bbox,:verified,:input_hash,:producer_version,:created_at)"
    ).bindparams(**_raw_occurrence(**changes))
    with pytest.raises(IntegrityError):
        db.write(statement)


def test_alias_global_uniqueness_foreign_keys_and_raw_empty_names_are_enforced(db):
    db.write(
        insert(concept_alias).values(
            normalized_name="known",
            concept_id="C1",
            name="Known",
            language=None,
            created_at="t",
        )
    )
    db.write(insert(concept).values(id="C2", name="Another", created_at="t"))
    with pytest.raises(IntegrityError, match="UNIQUE"):
        db.write(
            insert(concept_alias).values(
                normalized_name="known",
                concept_id="C2",
                name="Known",
                created_at="t",
            )
        )
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.write(
            insert(concept_alias).values(
                normalized_name="absent",
                concept_id="missing",
                name="Absent",
                created_at="t",
            )
        )
    with pytest.raises(IntegrityError, match="CHECK"):
        db.write(insert(concept).values(id="empty", name="   ", created_at="t"))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.write(insert(concept_occurrence).values(**_raw_occurrence(paper_id="absent")))


def test_private_lookup_and_public_occurrence_projection_track_raw_insert_delete(db):
    store = SqlStore(str(db._engine.url))
    try:
        db.write(
            insert(concept_alias).values(
                normalized_name="天文学",
                concept_id="C1",
                name="天文学",
                language="zh-CN",
                created_at="t",
            )
        )
        assert store.knowledge.count(None, "天文") == 1
        assert store.search.find("天文", 20) == ()
        db.write(
            insert(concept_occurrence).values(
                **_raw_occurrence(
                    definition="An author verified neutron definition.",
                    verified=True,
                    page=4,
                )
            )
        )
        (hit,) = store.search.find("neutron", 20)
        assert (hit.kind, hit.id, hit.paper_id, hit.page, hit.authorship) == (
            "concept",
            "C1",
            "P1",
            4,
            "author",
        )
        db.write(concept_alias.delete().where(concept_alias.c.normalized_name == "天文学"))
        assert store.knowledge.count(None, "天文") == 0
        assert store.search.find("neutron", 20)[0].page == 4
        db.write(concept_occurrence.delete().where(concept_occurrence.c.concept_id == "C1"))
        assert store.search.find("neutron", 20) == ()
        assert store.knowledge.count(None, "known") == 1
        db.write(concept.delete().where(concept.c.id == "C1"))
        assert store.knowledge.count(None, "known") == 0
        assert store.search.find("legacy", 20)
    finally:
        store.dispose()


def test_downgrade_removes_only_knowledge_and_reupgrade_preserves_qa_and_annotations(tmp_path):
    path = tmp_path / "roundtrip.db"
    url = _legacy(path)
    before, triggers = _snapshot(path)
    upgrade_database(url)
    store = SqlStore(url)
    item = store.knowledge.register("Temporary Concept", None, "t")
    store._db.write(insert(concept_occurrence).values(**_raw_occurrence(concept_id=item.id)))
    store.dispose()
    command.downgrade(alembic_config(url), "0010")
    after, current = _snapshot(path)
    assert before == after and triggers == current
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'concept%' "
                "OR name LIKE 'knowledge_%'"
            ).fetchall()
            == []
        )
        assert (
            conn.execute("SELECT qa_question_id FROM annotation WHERE id='N1'").fetchone()[0]
            == "Q1"
        )
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
    upgrade_database(url)
    after, _current = _snapshot(path)
    assert after == before
    store = SqlStore(url)
    try:
        assert store.knowledge.count(None, "") == 0
        assert store.qa.question("Q1").text == "Legacy user question"
        assert store.reader.get_annotation("N1").qa_question_id == "Q1"
    finally:
        store.dispose()
