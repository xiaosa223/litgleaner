"""Durable production source capture receipts and ordered input indexes.

Revision ID: 0021
Revises: 0020
"""

import sqlite3
from typing import cast

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_IDENTITY = (
    "id",
    "source",
    "query",
    "query_hash",
    "cursor",
    "purpose",
    "owner",
    "recipe",
    "started_at",
    "started_us",
    "rowid",
)


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def _attempt() -> None:
    _execute(
        "CREATE TABLE source_capture_attempt (id VARCHAR PRIMARY KEY NOT NULL,"
        'source VARCHAR NOT NULL,"query" TEXT NOT NULL,query_hash VARCHAR NOT NULL,'
        "cursor TEXT,purpose VARCHAR NOT NULL,owner TEXT NOT NULL,recipe TEXT,"
        "started_at VARCHAR NOT NULL,started_us BIGINT NOT NULL,status VARCHAR NOT NULL,"
        "finished_at VARCHAR,error_type VARCHAR,result TEXT,"
        "CHECK(length(trim(id))>0),CHECK(length(trim(source))>0),"
        "CHECK(length(trim(query_hash))>0),CHECK(length(trim(started_at))>0),"
        "CHECK(typeof(started_us)='integer'),"
        "CHECK(purpose IN ('discover','inbox','references','survey')),"
        "CHECK(status IN ('started','returned','raised')),"
        "CHECK(finished_at IS NULL OR length(trim(finished_at))>0),"
        "CHECK(error_type IS NULL OR length(trim(error_type))>0),"
        "CHECK((status='started' AND finished_at IS NULL "
        "AND error_type IS NULL AND result IS NULL) "
        "OR (status='returned' AND finished_at IS NOT NULL "
        "AND error_type IS NULL AND result IS NOT NULL) "
        "OR (status='raised' AND finished_at IS NOT NULL "
        "AND error_type IS NOT NULL AND result IS NULL)),"
        'CHECK(CASE WHEN json_valid("query") THEN json_type("query")=\'object\' ELSE 0 END),'
        "CHECK(CASE WHEN json_valid(owner) THEN json_type(owner)='object' ELSE 0 END),"
        "CHECK(recipe IS NULL OR CASE WHEN json_valid(recipe) "
        "THEN json_type(recipe)='object' ELSE 0 END),"
        "CHECK(result IS NULL OR CASE WHEN json_valid(result) "
        "THEN json_type(result)='object' ELSE 0 END))"
    )
    _execute(
        "CREATE INDEX ix_source_capture_attempt_source_id ON source_capture_attempt(source,id)"
    )
    _execute(
        "CREATE INDEX ix_source_capture_attempt_source_started "
        "ON source_capture_attempt(source,started_us)"
    )


def _attempt_guards() -> None:
    immutable = " OR ".join(f'new."{name}" IS NOT old."{name}"' for name in _IDENTITY)
    _execute(
        "CREATE TRIGGER source_capture_attempt_transition BEFORE UPDATE ON source_capture_attempt "
        f"WHEN old.status<>'started' OR new.status NOT IN ('returned','raised') OR {immutable} "
        "BEGIN SELECT RAISE(ABORT, 'source_capture_attempt_immutable'); END"
    )
    _execute(
        "CREATE TRIGGER source_capture_attempt_no_replace BEFORE INSERT "
        "ON source_capture_attempt WHEN new.status<>'started' OR EXISTS("
        "SELECT 1 FROM source_capture_attempt WHERE id=new.id OR rowid=new.rowid) "
        "BEGIN SELECT RAISE(ABORT, 'source_capture_attempt_immutable'); END"
    )


def _inputs() -> None:
    _execute(
        "CREATE TABLE source_capture_input (attempt_id VARCHAR NOT NULL,"
        "position INTEGER NOT NULL,kind VARCHAR NOT NULL,blob_key TEXT NOT NULL,"
        "sha256 VARCHAR NOT NULL,size BIGINT NOT NULL,PRIMARY KEY(attempt_id,position),"
        "CHECK(typeof(position)='integer' AND position>=0),"
        "CHECK(typeof(size)='integer' AND size>=0),"
        "CHECK(length(trim(kind))>0 AND length(trim(blob_key))>0),"
        "CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),"
        "FOREIGN KEY(attempt_id) REFERENCES source_capture_attempt(id) ON DELETE CASCADE)"
    )
    _execute(
        "CREATE TRIGGER source_capture_input_append BEFORE INSERT ON source_capture_input WHEN "
        "NOT EXISTS(SELECT 1 FROM source_capture_attempt "
        "WHERE id=new.attempt_id AND status='started') OR "
        "new.position<>COALESCE((SELECT MAX(position)+1 FROM source_capture_input "
        "WHERE attempt_id=new.attempt_id),0) OR "
        "EXISTS(SELECT 1 FROM source_capture_input WHERE rowid=new.rowid) "
        "BEGIN SELECT RAISE(ABORT, 'source_capture_input_append_invalid'); END"
    )
    _execute(
        "CREATE TRIGGER source_capture_input_no_update BEFORE UPDATE ON source_capture_input "
        "BEGIN SELECT RAISE(ABORT, 'source_capture_input_immutable'); END"
    )
    _execute(
        "CREATE TRIGGER source_capture_input_no_delete BEFORE DELETE ON source_capture_input "
        "WHEN EXISTS(SELECT 1 FROM source_capture_attempt WHERE id=old.attempt_id) "
        "BEGIN SELECT RAISE(ABORT, 'source_capture_input_immutable'); END"
    )


def _change(*, create: bool) -> None:
    connection = op.get_bind()
    raw = cast(sqlite3.Connection, connection.connection.driver_connection)
    if not raw.in_transaction:
        # pysqlite 不会为 DDL 自动开始物理事务，结构和版本号必须一起回滚。
        _execute("BEGIN IMMEDIATE")
    with connection.begin_nested():
        if create:
            _attempt()
            _attempt_guards()
            _inputs()
        else:
            op.drop_table("source_capture_input")
            op.drop_table("source_capture_attempt")


def upgrade() -> None:
    _change(create=True)


def downgrade() -> None:
    _change(create=False)
