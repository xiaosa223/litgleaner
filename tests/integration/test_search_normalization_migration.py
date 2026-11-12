"""搜索副本迁移保留原值、版本与全部规范触发器，可逆且失败回滚。"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from tests.integration.search_normalization_helpers import (
    COLUMNS,
    legacy_database,
    raw_rows,
    trigger_rows,
)

from litgleaner.domain.search_text import normalize_search_text
from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config, current_revision, upgrade_database
from litgleaner.infra.db.search_index import NORMALIZE_FUNCTION


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "旧搜索.db"
    url, db = legacy_database(path)
    yield path, url, db
    db.dispose()


def _assert_copies(db):
    rows = db.fetch(text("SELECT title,text,search_title,search_text FROM memory_fts"))
    assert rows
    for title, content, search_title, search_text in rows:
        assert search_title == normalize_search_text(title or "")
        assert search_text == normalize_search_text(content or "")


def test_old_rows_rowids_and_versions_survive_without_reading_archives(legacy):
    _, url, db = legacy
    before = raw_rows(db)
    upgrade_database(url)
    assert raw_rows(db) == before
    _assert_copies(db)
    columns = [row.name for row in db.fetch(text("PRAGMA table_info(memory_fts)"))]
    assert columns == [*COLUMNS.split(","), "search_title", "search_text"]
    sql = db.fetch(text("SELECT sql FROM sqlite_master WHERE name='memory_fts'"))[0].sql
    assert "title UNINDEXED,text UNINDEXED" in sql
    assert (
        db.fetch(text("SELECT source_version FROM memory_fts WHERE kind='document'"))[0][0]
        == "blob://original"
    )
    assert (
        db.fetch(text("SELECT count(*) FROM memory_fts WHERE memory_fts MATCH 'strasse'"))[0][0] > 0
    )


def test_downgrade_roundtrip_preserves_raw_rows_and_original_trigger_definitions(legacy):
    _, url, db = legacy
    before, triggers = raw_rows(db), trigger_rows(db)
    upgrade_database(url)
    assert len(trigger_rows(db)) == len(triggers) == 20
    command.downgrade(alembic_config(url), "0019")
    assert raw_rows(db) == before and trigger_rows(db) == triggers
    assert len(db.fetch(text("PRAGMA table_info(memory_fts)"))) == 8
    db.write(text("UPDATE annotation SET body='降级正文 Straße' WHERE id='N'"))
    after = raw_rows(db)
    upgrade_database(url)
    assert raw_rows(db) == after
    _assert_copies(db)


def test_every_canonical_trigger_rebuilds_normalized_copies_and_deletes_stale_rows(legacy):
    _, url, db = legacy
    upgrade_database(url)
    for sql in (
        "UPDATE paper SET title='ＡＬＰＨＡ Straße' WHERE id='P'",
        "INSERT INTO paper_alias VALUES ('external','ＦＵＬＬＷＩＤＴＨ','P')",
        "UPDATE paper_alias SET value='Straße' WHERE scheme='external'",
        "DELETE FROM paper_alias WHERE scheme='external'",
        "UPDATE annotation SET body='ＮＯＴＥ Café',quote='新 ＱＵＯＴＥ' WHERE id='N'",
        "INSERT INTO extraction(paper_id,input_hash,extractor_version,data,created_at) "
        "VALUES ('P','i2','v2','{\"summary\":\"ＮＥＷ Straße\"}','t2')",
        "DELETE FROM extraction WHERE input_hash='i2'",
        "INSERT INTO qa_message(id,paper_id,role,request_id,text,data,created_at) "
        "VALUES ('Q2','P','question','r2','ＮＥＷ Frage Straße','{}','t2')",
        "DELETE FROM qa_message WHERE id='Q2'",
        "INSERT INTO concept_alias VALUES ('newalias','C','ＮＥＷ Alias','en','t')",
        "DELETE FROM concept_alias WHERE normalized_name='newalias'",
    ):
        db.write(text(sql))
        _assert_copies(db)
    db.write(text("UPDATE annotation SET deleted_at='t3' WHERE id='N'"))
    assert not db.fetch(text("SELECT id FROM memory_fts WHERE id LIKE 'N:%'"))
    db.write(text("DELETE FROM concept_occurrence WHERE paper_id='P'"))
    assert not db.fetch(text("SELECT id FROM memory_fts WHERE kind='concept'"))
    db.write(text("DELETE FROM concept_alias WHERE concept_id='C'"))
    db.write(text("DELETE FROM concept WHERE id='C'"))
    assert not db.fetch(text("SELECT id FROM memory_fts WHERE kind='concept_name'"))


def test_new_records_and_all_delete_paths_keep_search_copies_consistent(legacy):
    _, url, db = legacy
    upgrade_database(url)
    statements = (
        "INSERT INTO paper(id,title,title_key,authors,first_seen_at) "
        "VALUES ('P2','ＮＥＷ Straße','p2','[]','t')",
        "INSERT INTO annotation(id,paper_id,kind,quote,body,created_at,updated_at) "
        "VALUES ('N2','P2','highlight','ＡＵＴＨＯＲ','ＵＳＥＲ','t','t')",
        "INSERT INTO extraction(paper_id,input_hash,extractor_version,data,created_at) "
        "VALUES ('P2','new','v','{\"summary\":\"ＦＡＣＴ\"}','t')",
        "UPDATE extraction SET data='{\"summary\":\"ＵＰＤＡＴＥＤ\"}' WHERE paper_id='P2'",
        "INSERT INTO concept VALUES ('C2','ＮＥＷ ＣＯＮＣＥＰＴ','t')",
        "INSERT INTO concept_occurrence VALUES ('C2','P2','core',NULL,2,NULL,0,'new','v','t')",
        "DELETE FROM concept_occurrence WHERE concept_id='C2'",
        "DELETE FROM concept WHERE id='C2'",
        "DELETE FROM annotation WHERE id='N2'",
        "DELETE FROM extraction WHERE paper_id='P2'",
        "DELETE FROM paper WHERE id='P2'",
    )
    for statement in statements:
        db.write(text(statement))
        _assert_copies(db)
    assert not db.fetch(text("SELECT id FROM memory_fts WHERE paper_id='P2' OR id='C2'"))


@pytest.mark.parametrize(
    "failure",
    ["DROP TABLE memory_fts", "CREATE VIRTUAL TABLE memory_fts", "CREATE TRIGGER qa_search_insert"],
)
def test_ddl_failures_leave_version_original_rows_and_triggers_intact(legacy, failure):
    _, url, db = legacy
    before, triggers = raw_rows(db), trigger_rows(db)

    def fail(_conn, _cursor, statement, _params, _context, _many):
        if statement.startswith(failure):
            raise RuntimeError("injected normalization migration failure")

    event.listen(Engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            upgrade_database(url)
    finally:
        event.remove(Engine, "before_cursor_execute", fail)
    assert current_revision(url) == "0019"
    assert raw_rows(db) == before and trigger_rows(db) == triggers
    assert len(db.fetch(text("PRAGMA table_info(memory_fts)"))) == 8
    upgrade_database(url)
    _assert_copies(db)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO paper(id,title,title_key,authors,first_seen_at) "
        "VALUES ('bare','bad','bare','[]','t')",
        "UPDATE paper SET title='bad' WHERE id='P'",
        "DELETE FROM paper WHERE id='P'",
        "DELETE FROM annotation WHERE id='N'",
        "DELETE FROM qa_message WHERE id='A'",
    ],
)
def test_plain_sqlite_can_read_and_check_but_unregistered_canonical_write_fails_closed(
    legacy, statement
):
    path, url, db = legacy
    upgrade_database(url)
    before = raw_rows(db)
    canonical = {
        name: db.fetch(text(f"SELECT * FROM {name} ORDER BY rowid"))
        for name in ("paper", "annotation", "qa_message")
    }
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == len(before)
        connection.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
        with pytest.raises(sqlite3.OperationalError, match="no such function"):
            connection.execute(statement)
        connection.rollback()
    assert raw_rows(db) == before
    assert canonical == {
        name: db.fetch(text(f"SELECT * FROM {name} ORDER BY rowid")) for name in canonical
    }


def test_new_business_connections_register_deterministic_function_for_null_and_unicode(legacy):
    _, url, _db = legacy
    upgrade_database(url)
    fresh = Database(url)
    try:
        assert fresh.fetch(text(f"SELECT {NORMALIZE_FUNCTION}(NULL)"))[0][0] == ""
        assert (
            fresh.fetch(text(f"SELECT {NORMALIZE_FUNCTION}('  Ｓｔｒａße　é  ')"))[0][0]
            == "strasse é"
        )
        rows = fresh.fetch(text("PRAGMA function_list"))
        assert any(row.name == NORMALIZE_FUNCTION and row.flags & 0x800 for row in rows)
        fresh.write(text("UPDATE paper SET title='ＦＲＥＳＨ' WHERE id='P'"))
        _assert_copies(fresh)
    finally:
        fresh.dispose()
