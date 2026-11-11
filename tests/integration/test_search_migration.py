"""FTS5 独立验收：旧数据回填、触发器、影子表、升级和降级。"""

import json
import sqlite3
from pathlib import Path

from alembic import command
from sqlalchemy import MetaData, Table

from litgleaner.infra.db.database import Database
from litgleaner.infra.db.migrate import alembic_config, upgrade_database
from litgleaner.infra.db.schema_rules import FTS_TABLES, include_schema_object
from litgleaner.infra.db.search_repo import SqlSearchRepo


def _old_database(path: Path) -> str:
    url = f"sqlite:///{path}"
    command.upgrade(alembic_config(url), "0007")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO paper(id,title,title_key,abstract,authors,venue,first_seen_at) "
            "VALUES ('P1','Legacy Astronomy','legacyastronomy','Ancient receptor',?,"
            " 'Old Venue','2026-01-01')",
            (json.dumps(["Legacy Author"]),),
        )
        connection.execute("INSERT INTO paper_alias VALUES ('doi','10.1000/legacy-alias','P1')")
        connection.execute(
            "INSERT INTO annotation(id,paper_id,kind,page,quote,body,created_at,updated_at) "
            "VALUES ('A1','P1','note',5,'An original nebula quote','用户旧笔记','t','t')"
        )
        connection.execute(
            "INSERT INTO annotation(id,paper_id,kind,body,created_at,updated_at,deleted_at) "
            "VALUES ('A2','P1','note','Deleted legacy annotation','t','t','t')"
        )
        for index, summary in enumerate(("Outdated extraction", "Current spectroscopy")):
            connection.execute(
                "INSERT INTO extraction(paper_id,input_hash,extractor_version,data,created_at) "
                "VALUES ('P1',?,'v1',?,'t')",
                (f"hash{index}", json.dumps({"summary": summary})),
            )
    return url


def test_0008_backfills_legacy_papers_live_annotations_and_latest_extractions(tmp_path: Path):
    path = tmp_path / "legacy.db"
    url = _old_database(path)
    upgrade_database(url)
    db = Database(url)
    repo = SqlSearchRepo(db)
    try:
        for query in ("Astronomy", "Legacy Author", "Old Venue", "receptor", "legacy-alias"):
            hit = repo.find(query, 20)[0]
            assert (hit.kind, hit.paper_id, hit.authorship) == ("paper", "P1", "author")
        body = repo.find("旧笔记", 20)[0]
        quote = repo.find("nebula", 20)[0]
        assert (body.id, body.page, body.authorship) == ("A1:body", 5, "user")
        assert (quote.id, quote.page, quote.authorship) == ("A1:quote", 5, "author")
        assert repo.find("spectroscopy", 20)[0].authorship == "model"
        assert repo.find("Outdated", 20) == ()
        assert repo.find("Deleted", 20) == ()
    finally:
        db.dispose()


def test_fts_virtual_table_shadow_tables_and_integrity_are_verified_separately(tmp_path: Path):
    path = tmp_path / "fts.db"
    url = _old_database(path)
    upgrade_database(url)
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables >= FTS_TABLES
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='memory_fts'"
        ).fetchone()[0]
        assert "fts5" in sql.lower() and "trigram" in sql.lower()
        assert connection.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 4
        assert (
            connection.execute(
                "SELECT count(*) FROM memory_fts WHERE memory_fts MATCH ?", ('"Astronomy"',)
            ).fetchone()[0]
            == 1
        )
        connection.execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")
        triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'search_%'"
        ).fetchall()
        assert len(triggers) == 12


def test_fts_schema_filter_excludes_only_the_known_virtual_and_shadow_tables():
    metadata = MetaData()
    for name in FTS_TABLES:
        assert not include_schema_object(
            Table(name, metadata, extend_existing=True), name, "table", True, None
        )
    assert include_schema_object(
        Table("memory_fts_future", metadata), "memory_fts_future", "table", True, None
    )


def test_downgrade_removes_fts_and_triggers_and_reupgrade_rebuilds_from_facts(tmp_path: Path):
    path = tmp_path / "roundtrip.db"
    url = _old_database(path)
    upgrade_database(url)
    command.downgrade(alembic_config(url), "0007")
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert not tables.intersection(FTS_TABLES)
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'search_%'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT count(*) FROM paper").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM annotation").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM extraction").fetchone()[0] == 2
    upgrade_database(url)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM memory_fts").fetchone()[0] == 4
        assert (
            connection.execute("SELECT text FROM memory_fts WHERE authorship='user'").fetchone()[0]
            == "用户旧笔记"
        )
