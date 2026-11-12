"""0015 增量迁移保留原表、FTS、外键与旧费用。"""

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
from litgleaner.infra.db.schema_rules import FTS_TABLES, include_schema_object
from litgleaner.infra.db.store import SqlStore


def _legacy(path):
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0014")
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO paper(id,title,title_key,authors,first_seen_at) "
            "VALUES ('P','Old','old','[]','t')"
        )
        conn.execute("INSERT INTO paper_alias VALUES ('doi','10.1000/old','P')")
        conn.execute(
            "INSERT INTO qa_message(id,paper_id,role,request_id,text,data,created_at) "
            "VALUES ('Q','P','question','R','Original question','{}','t')"
        )
        conn.execute(
            "INSERT INTO annotation(id,paper_id,kind,body,qa_question_id,created_at,updated_at) "
            "VALUES ('N','P','note','Original note','Q','t','t')"
        )
        conn.execute("INSERT INTO concept VALUES ('C','Old Concept','t')")
        conn.execute("INSERT INTO concept_alias VALUES ('old concept','C','Old Concept',NULL,'t')")
        conn.execute(
            "INSERT INTO concept_occurrence "
            "VALUES ('C','P','core','An original verified definition.',0,NULL,1,'h','v','t')"
        )
        conn.execute(
            "INSERT INTO citation(citing_paper_id,position,raw_text,source,source_position,"
            "cited_aliases,contexts,intents,created_at) "
            "VALUES ('P','api:0','Old ref','api',0,'[]','[]','[]','t')"
        )
        conn.execute(
            "INSERT INTO llm_call(task,model,prompt_version,input_tokens,output_tokens,cost_usd,"
            "ok,created_at,paper_id) VALUES ('triage','old','v',1,2,0.25,1,'t','P')"
        )
    return url


def _snapshot(path):
    with sqlite3.connect(path) as conn:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if row[0] not in FTS_TABLES - {"memory_fts"}
            and row[0] != "alembic_version"
            and not row[0].startswith("survey_")
            and row[0] not in {"topic_map", "topic_outline", "paper_embedding"}
        ]
        rows = {}
        for name in tables:
            columns = (
                "*"
                if name != "llm_call"
                else "id,task,model,prompt_version,input_tokens,output_tokens,"
                "cost_usd,ok,created_at,paper_id"
            )
            rows[name] = conn.execute(f"SELECT {columns} FROM {name} ORDER BY rowid").fetchall()
        triggers = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
        foreign_keys = {
            name: conn.execute(f"PRAGMA foreign_key_list({name})").fetchall() for name in tables
        }
    return rows, triggers, foreign_keys


def test_0015_upgrade_preserves_old_facts_triggers_foreign_keys_and_matches_schema(tmp_path):
    path = tmp_path / "old.db"
    url = _legacy(path)
    before, triggers, foreign_keys = _snapshot(path)
    upgrade_database(url)
    upgrade_database(url)
    after, current, current_keys = _snapshot(path)
    assert before == after and foreign_keys == current_keys
    assert all(current[name] == sql for name, sql in triggers.items())
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"include_object": include_schema_object}
            )
            assert compare_metadata(context, metadata) == []
            assert conn.exec_driver_sql("SELECT scope_id FROM llm_call").scalar() is None
            assert "ix_llm_call_scope_id" in {
                row[1] for row in conn.exec_driver_sql("PRAGMA index_list(llm_call)")
            }
    finally:
        engine.dispose()
    store = SqlStore(url)
    try:
        assert store.runs.llm_cost_for_scope("missing") == 0
        assert store.runs.llm_cost_for_paper("P") == 0.25
        assert store.reader.get_annotation("N").qa_question_id == "Q"
        assert store.knowledge.resolve("old concept").id == "C"
        assert store.citations.outgoing("P")[0].raw_text == "Old ref"
    finally:
        store.dispose()


@pytest.mark.parametrize("value", ["broken JSON", "null", "[]", "{}"])
def test_preview_json_and_identity_constraints_reject_invalid_content(tmp_path, value):
    url = f"sqlite:///{tmp_path / 'invalid.db'}"
    upgrade_database(url)
    db = Database(url)
    try:
        with pytest.raises(IntegrityError):
            db.write(
                text(
                    "INSERT INTO survey_preview(id,topic_id,topic_hash,"
                    "fingerprint,created_at,data) "
                    "VALUES ('p','topic','hash','f','t',:data)"
                ).bindparams(data=value)
            )
    finally:
        db.dispose()


def test_0015_downgrade_only_removes_new_tables_scope_index_and_own_triggers(tmp_path):
    path = tmp_path / "roundtrip.db"
    url = _legacy(path)
    before, triggers, foreign_keys = _snapshot(path)
    upgrade_database(url)
    command.downgrade(alembic_config(url), "0014")
    after, current, current_keys = _snapshot(path)
    assert after == before and current == triggers and current_keys == foreign_keys
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'survey_%'").fetchall()
            == []
        )
        assert "scope_id" not in {row[1] for row in conn.execute("PRAGMA table_info(llm_call)")}
        conn.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
    upgrade_database(url)
    store = SqlStore(url)
    try:
        assert store.surveys.rounds("topic") == ()
        assert store.runs.llm_cost_for_paper("P") == 0.25
    finally:
        store.dispose()
