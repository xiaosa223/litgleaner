"""0012 引文迁移：追加约束、JSON 类型、外键及旧事实/FTS 兼容。"""

import sqlite3

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, insert, text
from sqlalchemy.exc import IntegrityError

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.infra.db.schema import citation, metadata, paper
from litgleaner.infra.db.schema_rules import include_schema_object
from litgleaner.infra.db.store import SqlStore


def _legacy(path):
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0011")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO paper(id,title,title_key,authors,first_seen_at) "
            "VALUES ('P1','Legacy Paper','legacy','[]','t')"
        )
        conn.execute("INSERT INTO paper_alias VALUES ('doi','10.1000/legacy','P1')")
        conn.execute(
            "INSERT INTO qa_message(id,paper_id,role,request_id,text,data,created_at) "
            "VALUES ('Q','P1','question','R','My original question','{}','t')"
        )
        conn.execute(
            "INSERT INTO annotation(id,paper_id,kind,body,qa_question_id,created_at,updated_at) "
            "VALUES ('N','P1','note','My original note','Q','t','t')"
        )
        conn.execute("INSERT INTO concept VALUES ('C','Legacy Concept','t')")
        conn.execute(
            "INSERT INTO concept_alias VALUES ('legacy concept','C','Legacy Concept',NULL,'t')"
        )
        conn.execute(
            "INSERT INTO concept_occurrence VALUES "
            "('C','P1','core','An author verified definition.',2,NULL,1,'h','v','t')"
        )
    return url


def _snapshot(path):
    tables = (
        "paper",
        "paper_alias",
        "qa_message",
        "annotation",
        "concept",
        "concept_alias",
        "concept_occurrence",
        "memory_fts",
    )
    with sqlite3.connect(path) as conn:
        rows = {}
        for name in tables:
            columns = (
                "id,title,title_key,abstract,authors,venue,issn,published_at,first_seen_at"
                if name == "paper"
                else "*"
            )
            rows[name] = conn.execute(f"SELECT {columns} FROM {name} ORDER BY rowid").fetchall()
        triggers = dict(
            conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'").fetchall()
        )
        return rows, triggers


def _raw(**changes):
    return {
        "citing_paper_id": "P1",
        "position": "api:0:hash",
        "raw_text": "Original reference",
        "source": "api",
        "source_position": 0,
        "cited_paper_id": None,
        "cited_title": None,
        "cited_aliases": "[]",
        "contexts": "[]",
        "intents": "[]",
        "influential": None,
        "created_at": "t",
        "page": None,
        "bbox": None,
        **changes,
    }


@pytest.fixture
def db(tmp_path):
    url = _legacy(tmp_path / "citation.db")
    upgrade_database(url)
    database = Database(url)
    yield database
    database.dispose()


def test_0012_upgrade_preserves_old_facts_triggers_and_matches_metadata(tmp_path):
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
            foreign = connection.exec_driver_sql("PRAGMA foreign_key_list(citation)").all()
            assert {row[3:5] for row in foreign} == {
                ("cited_paper_id", "id"),
                ("citing_paper_id", "id"),
            }
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "changes",
    [
        {"position": ""},
        {"position": "   "},
        {"source": ""},
        {"source_position": -1},
        {"source_position": None},
        {"source_position": 1.5},
        {"cited_aliases": "broken JSON"},
        {"cited_aliases": "{}"},
        {"cited_aliases": "[1]"},
        {"cited_aliases": '[{"scheme":"doi"}]'},
        {"cited_aliases": '[{"scheme":"doi","value":1}]'},
        {"contexts": "broken JSON"},
        {"contexts": "{}"},
        {"contexts": "[1]"},
        {"contexts": "[null]"},
        {"contexts": "[{}]"},
        {"intents": "[true]"},
        {"influential": 2},
        {"influential": -1},
        {"influential": "unknown"},
        {"page": -1},
        {"page": 1.5},
        {"bbox": "broken JSON"},
        {"bbox": "{}"},
    ],
)
def test_database_rejects_invalid_position_json_elements_boolean_and_geometry(db, changes):
    values = _raw(**changes)
    columns = ",".join(values)
    parameters = ",".join(f":{name}" for name in values)
    with pytest.raises(IntegrityError):
        db.write(
            text(f"INSERT INTO citation({columns}) VALUES ({parameters})").bindparams(**values)
        )


def test_database_checks_candidate_shape_before_ignoring_duplicate_key(db):
    db.write(insert(citation).values(**_raw()))
    for changes in ({"contexts": "[1]"}, {"page": -1}, {"position": ""}):
        values = _raw(**changes)
        with pytest.raises(IntegrityError):
            db.write(
                text(
                    "INSERT INTO citation(citing_paper_id,position,raw_text,source,source_position,"
                    "cited_aliases,contexts,intents,influential,created_at,page) "
                    "VALUES (:citing_paper_id,:position,:raw_text,:source,:source_position,"
                    ":cited_aliases,:contexts,:intents,:influential,:created_at,:page) "
                    "ON CONFLICT(citing_paper_id,position) DO NOTHING"
                ).bindparams(
                    **{
                        key: value
                        for key, value in values.items()
                        if key not in {"cited_paper_id", "cited_title", "bbox"}
                    }
                )
            )


@pytest.mark.parametrize("link", ["citing_paper_id", "cited_paper_id"])
def test_foreign_keys_prevent_deleting_a_recorded_target_or_citing_paper(db, link):
    db.write(
        insert(paper).values(
            id="linked",
            title="Linked",
            title_key="linked",
            authors="[]",
            first_seen_at="t",
        )
    )
    db.write(insert(citation).values(**_raw(**{link: "linked"})))
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        db.write(text("DELETE FROM paper WHERE id='linked'"))


def test_inserting_references_does_not_change_any_existing_fts_record_or_trigger(tmp_path):
    path = tmp_path / "insert.db"
    url = _legacy(path)
    upgrade_database(url)
    before, triggers = _snapshot(path)
    db = Database(url)
    try:
        for ordinal in range(20):
            db.write(
                insert(citation).values(
                    **_raw(position=f"api:{ordinal}:hash", source_position=ordinal)
                )
            )
    finally:
        db.dispose()
    after, current = _snapshot(path)
    assert before == after and triggers == current
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")


def test_0012_downgrade_cleans_only_its_table_and_triggers_and_reupgrade_is_empty(tmp_path):
    path = tmp_path / "roundtrip.db"
    url = _legacy(path)
    before, triggers = _snapshot(path)
    upgrade_database(url)
    db = Database(url)
    db.write(insert(citation).values(**_raw()))
    db.dispose()
    command.downgrade(alembic_config(url), "0011")
    after, current = _snapshot(path)
    assert after == before and current == triggers
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'citation%'").fetchall()
            == []
        )
    upgrade_database(url)
    store = SqlStore(url)
    try:
        assert store.citations.outgoing("P1") == ()
        assert store.reader.get_annotation("N").qa_question_id == "Q"
        assert store.knowledge.resolve("legacy concept").id == "C"
    finally:
        store.dispose()
