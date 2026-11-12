"""0013 只增加论文元数据列，保留已存在的事实和搜索触发器。"""

import sqlite3

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from tests.integration.relation_migration_helpers import (
    FTS_RAW_COLUMNS,
    assert_search_upgrade_triggers,
)

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.infra.db.schema import metadata
from litgleaner.infra.db.schema_rules import include_schema_object
from litgleaner.infra.db.search_index import register_search_functions
from litgleaner.infra.db.store import SqlStore

_OLD_PAPER_COLUMNS = "id,title,title_key,abstract,authors,venue,issn,published_at,first_seen_at"


def _legacy(path):
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0012")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO paper(id,title,title_key,authors,first_seen_at) "
            "VALUES ('P','Legacy Study','legacy','[]','t')"
        )
        conn.execute("INSERT INTO paper_alias VALUES ('doi','10.1000/legacy','P')")
        conn.execute(
            "INSERT INTO qa_message(id,paper_id,role,request_id,text,data,created_at) "
            "VALUES ('Q','P','question','R','Original user question','{}','t')"
        )
        conn.execute(
            "INSERT INTO annotation(id,paper_id,kind,body,qa_question_id,created_at,updated_at) "
            "VALUES ('N','P','note','Original user note','Q','t','t')"
        )
        conn.execute("INSERT INTO concept VALUES ('C','Original Concept','t')")
        conn.execute(
            "INSERT INTO concept_alias VALUES ('original concept','C','Original Concept',NULL,'t')"
        )
        conn.execute(
            "INSERT INTO concept_occurrence VALUES "
            "('C','P','core','A verified original definition.',0,NULL,1,'h','v','t')"
        )
        conn.execute(
            "INSERT INTO citation(citing_paper_id,position,raw_text,source,source_position,"
            "cited_aliases,contexts,intents,created_at) "
            "VALUES ('P','api:0:hash','Original reference','api',0,'[]','[]','[]','t')"
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
        "citation",
        "memory_fts",
    )
    with sqlite3.connect(path) as conn:
        rows = {}
        for table in tables:
            columns = (
                _OLD_PAPER_COLUMNS
                if table == "paper"
                else FTS_RAW_COLUMNS
                if table == "memory_fts"
                else "*"
            )
            order = "kind,id,paper_id" if table == "memory_fts" else "rowid"
            rows[table] = conn.execute(f"SELECT {columns} FROM {table} ORDER BY {order}").fetchall()
        triggers = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    return rows, triggers


def test_0013_upgrades_existing_paper_defaults_and_preserves_all_prior_facts(tmp_path):
    path = tmp_path / "old.db"
    url = _legacy(path)
    before, triggers = _snapshot(path)
    upgrade_database(url)
    upgrade_database(url)
    after, current = _snapshot(path)
    assert before == after
    assert_search_upgrade_triggers(triggers, current)
    store = SqlStore(url)
    try:
        old = store.papers.get("P")
        assert old.publication_types == () and old.citation_count is None
    finally:
        store.dispose()
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"include_object": include_schema_object}
            )
            assert compare_metadata(context, metadata) == []
    finally:
        engine.dispose()


@pytest.fixture
def db(tmp_path):
    url = _legacy(tmp_path / "invalid.db")
    upgrade_database(url)
    database = Database(url)
    yield database
    database.dispose()


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("publication_types", "broken JSON"),
        ("publication_types", "{}"),
        ("publication_types", '"Review"'),
        ("publication_types", "null"),
        ("publication_types", "[1]"),
        ("publication_types", "[null]"),
        ("publication_types", "[true]"),
        ("publication_types", '[""]'),
        ("publication_types", '["   "]'),
        ("publication_types", '["Review", {}]'),
        ("citation_count", -1),
        ("citation_count", 1.5),
        ("citation_count", "many"),
    ],
)
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_constraints_reject_invalid_facts_on_insert_and_update(db, column, value, operation):
    if operation == "insert":
        sql = (
            "INSERT INTO paper(id,title,title_key,authors,first_seen_at," + column + ") "
            "VALUES ('invalid','T','t','[]','t',:value)"
        )
    else:
        sql = f"UPDATE paper SET {column}=:value WHERE id='P'"
    with pytest.raises(IntegrityError):
        db.write(text(sql).bindparams(value=value))


def test_0013_downgrade_removes_only_new_columns_and_validators_then_can_reupgrade(tmp_path):
    path = tmp_path / "roundtrip.db"
    url = _legacy(path)
    before, triggers = _snapshot(path)
    upgrade_database(url)
    with sqlite3.connect(path) as conn:
        register_search_functions(conn)
        conn.execute("UPDATE paper SET publication_types='[\"Review\"]',citation_count=0")
    updated_fts = _snapshot(path)[0]["memory_fts"]
    assert [row[1:] for row in updated_fts] == [row[1:] for row in before["memory_fts"]]
    command.downgrade(alembic_config(url), "0012")
    after, current = _snapshot(path)
    assert after == {**before, "memory_fts": updated_fts} and current == triggers
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(paper)")}
        assert not columns & {"publication_types", "citation_count"}
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
    upgrade_database(url)
    reupgraded, current = _snapshot(path)
    assert reupgraded == after
    assert_search_upgrade_triggers(triggers, current)
    store = SqlStore(url)
    try:
        assert store.papers.get("P").publication_types == ()
        assert store.papers.get("P").citation_count is None
        assert store.reader.get_annotation("N").qa_question_id == "Q"
        assert store.knowledge.resolve("original concept").id == "C"
        assert store.citations.outgoing("P")[0].raw_text == "Original reference"
    finally:
        store.dispose()
