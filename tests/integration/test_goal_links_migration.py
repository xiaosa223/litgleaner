"""0014 原生增加 goal_links，旧 assessment、分数和搜索索引完整保留。"""

import json
import sqlite3

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.infra.db.schema import metadata
from litgleaner.infra.db.schema_rules import include_schema_object
from litgleaner.infra.db.store import SqlStore

_ASSESSMENT_COLUMNS = (
    "id,paper_id,topic_id,topic_hash,semantic_hash,input_hash,levels,evidence,"
    "fulltext_used,model,prompt_version,created_at"
)


def _legacy(path):
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0013")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO paper(id,title,title_key,authors,first_seen_at,"
            "publication_types,citation_count) "
            "VALUES ('P','Legacy Study','legacy','[]','t','[\"Review\"]',0)"
        )
        conn.execute(
            "INSERT INTO assessment(paper_id,topic_id,topic_hash,semantic_hash,input_hash,"
            "levels,evidence,fulltext_used,model,prompt_version,created_at) "
            "VALUES ('P','topic','hash','semantic','input','{\"relevance\":4}',"
            "'{\"reason\":\"Original evidence\"}',1,'model','v','t')"
        )
        conn.execute(
            "INSERT INTO score(assessment_id,policy_hash,score,tier,provisional,"
            "undetermined,created_at) "
            "VALUES (1,'policy',80,'must_read',0,'[]','t')"
        )
        conn.execute(
            "INSERT INTO annotation(id,paper_id,kind,body,created_at,updated_at) "
            "VALUES ('N','P','note','Original user note','t','t')"
        )
    return url


def _snapshot(path):
    with sqlite3.connect(path) as conn:
        tables = ("paper", "assessment", "score", "annotation", "memory_fts")
        rows = {}
        for table in tables:
            columns = _ASSESSMENT_COLUMNS if table == "assessment" else "*"
            rows[table] = conn.execute(f"SELECT {columns} FROM {table} ORDER BY rowid").fetchall()
        triggers = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    return rows, triggers


def test_0014_upgrade_preserves_old_assessment_scores_metadata_and_triggers(tmp_path):
    path = tmp_path / "legacy.db"
    url = _legacy(path)
    before, triggers = _snapshot(path)
    upgrade_database(url)
    upgrade_database(url)
    after, current = _snapshot(path)
    assert before == after and all(current[name] == sql for name, sql in triggers.items())
    store = SqlStore(url)
    try:
        assert store.judgments.get_assessment(1).goal_links == ()
        assert store.judgments.scored_for_paper("P")[0].score == 80
    finally:
        store.dispose()
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"include_object": include_schema_object}
            )
            assert compare_metadata(context, metadata) == []
            column = next(
                row
                for row in connection.exec_driver_sql("PRAGMA table_info(assessment)")
                if row[1] == "goal_links"
            )
            assert column[3] == 1 and column[4] == "'[]'"
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
    "value",
    [
        None,
        "broken JSON",
        "null",
        "{}",
        '"text"',
        "[1]",
        "[null]",
        '["text"]',
        "[{}]",
        json.dumps([{"goal_id": 1, "reuse": "reason"}]),
        json.dumps([{"goal_id": "g", "reuse": 1}]),
        json.dumps([{"goal_id": "g", "reuse": "reason", "quote": 4}]),
        json.dumps([{"goal_id": "g", "reuse": "reason", "page": -1}]),
        json.dumps([{"goal_id": "g", "reuse": "reason", "page": 1.5}]),
        json.dumps([{"goal_id": "g", "reuse": "reason", "page": True}]),
        json.dumps([{"goal_id": "g", "reuse": "reason", "verified": "true"}]),
    ],
)
@pytest.mark.parametrize("operation", ["insert", "update"])
def test_constraints_reject_bad_json_array_elements_and_field_types(db, value, operation):
    if operation == "insert":
        sql = (
            "INSERT INTO assessment(paper_id,topic_id,topic_hash,semantic_hash,input_hash,"
            "levels,evidence,fulltext_used,model,prompt_version,created_at,goal_links) "
            "VALUES ('P','other','hash','s','i','{}','{}',0,'m','v','t',:value)"
        )
    else:
        sql = "UPDATE assessment SET goal_links=:value WHERE id=1"
    with pytest.raises(IntegrityError):
        db.write(text(sql).bindparams(value=value))


def test_0014_downgrade_preserves_old_values_then_reupgrade_restores_empty_default(tmp_path):
    path = tmp_path / "roundtrip.db"
    url = _legacy(path)
    before, triggers = _snapshot(path)
    upgrade_database(url)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE assessment SET goal_links=?",
            (json.dumps([{"goal_id": "g1", "reuse": "A recommendation", "verified": False}]),),
        )
    command.downgrade(alembic_config(url), "0013")
    after, current = _snapshot(path)
    assert after == before and current == triggers
    with sqlite3.connect(path) as conn:
        assert "goal_links" not in {row[1] for row in conn.execute("PRAGMA table_info(assessment)")}
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
    upgrade_database(url)
    store = SqlStore(url)
    try:
        assert store.judgments.get_assessment(1).goal_links == ()
        assert store.papers.get("P").publication_types == ("Review",)
        assert store.papers.get("P").citation_count == 0
    finally:
        store.dispose()
