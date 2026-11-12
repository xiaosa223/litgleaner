"""Separate normalized search copies from immutable original text.

Revision ID: 0020
Revises: 0019
"""

import sqlite3
from importlib import import_module
from typing import cast

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_COLUMNS = "kind,id,paper_id,page,authorship,source_version,title,text"
_FUNCTION = "litgleaner_search_normalize_v1"
_COPY = "_memory_fts_normalization_copy"
_BASE = "litgleaner.infra.db.migrations.versions."


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def _projection(body: str, *, normalized: bool) -> str:
    if not normalized:
        return body
    prefix = f"INSERT INTO memory_fts({_COLUMNS}) "
    statements = []
    for statement in body.split(";"):
        if not statement.strip():
            continue
        projected = statement
        if statement.startswith(prefix):
            original = statement[len(prefix) :]
            projected = (
                f"INSERT INTO memory_fts({_COLUMNS},search_title,search_text) "
                f"SELECT projected.*,{_FUNCTION}(projected.title),"
                f"{_FUNCTION}(projected.text) FROM (WITH projected({_COLUMNS}) AS "
                f"({original}) SELECT * FROM projected) AS projected"
            )
        statements.append(projected + ";")
    return f"SELECT {_FUNCTION}(NULL);" + "".join(statements)


def _trigger(name: str, event: str, table: str, body: str, *, normalized: bool) -> None:
    _execute(
        f"CREATE TRIGGER {name} AFTER {event} ON {table} "
        f"BEGIN {_projection(body, normalized=normalized)} END"
    )


def _fact_triggers(*, normalized: bool) -> None:
    legacy = import_module(_BASE + "0008_memory_fts")
    for table in ("paper", "paper_alias"):
        for event in ("INSERT", "UPDATE", "DELETE"):
            alias = "old" if event == "DELETE" else "new"
            key = f"{alias}.id" if table == "paper" else f"{alias}.paper_id"
            body = f"DELETE FROM memory_fts WHERE kind='paper' AND paper_id={key};"
            if table != "paper" or event != "DELETE":
                body += cast(str, legacy._paper(f"p.id={key}"))
            _trigger(f"search_{table}_{event.lower()}", event, table, body, normalized=normalized)
    for event in ("INSERT", "UPDATE", "DELETE"):
        alias = "old" if event == "DELETE" else "new"
        body = (
            f"DELETE FROM memory_fts WHERE id IN ({alias}.id || ':body', {alias}.id || ':quote') "
            "AND kind IN ('note','highlight');"
        )
        if event != "DELETE":
            body += cast(str, legacy._annotation("a.id=new.id"))
        _trigger(
            f"search_annotation_{event.lower()}", event, "annotation", body, normalized=normalized
        )
        body = f"DELETE FROM memory_fts WHERE kind='extraction' AND paper_id={alias}.paper_id;"
        body += cast(str, legacy._extraction(f"e.paper_id={alias}.paper_id"))
        _trigger(
            f"search_extraction_{event.lower()}", event, "extraction", body, normalized=normalized
        )


def _memory_triggers(*, normalized: bool) -> None:
    body = (
        f"INSERT INTO memory_fts({_COLUMNS}) VALUES ('qa',new.id,new.paper_id,new.page,"
        "CASE WHEN new.role='question' THEN 'user' ELSE 'model' END,'','',new.text);"
    )
    _trigger("qa_search_insert", "INSERT", "qa_message", body, normalized=normalized)
    _trigger(
        "qa_search_delete",
        "DELETE",
        "qa_message",
        "DELETE FROM memory_fts WHERE kind='qa' AND id=old.id;",
        normalized=normalized,
    )
    legacy = import_module(_BASE + "0011_knowledge_facts")
    for table in ("concept", "concept_alias", "concept_occurrence"):
        for event in ("INSERT", "DELETE"):
            alias = "new" if event == "INSERT" else "old"
            key = f"{alias}.id" if table == "concept" else f"{alias}.concept_id"
            _trigger(
                f"knowledge_search_{table}_{event.lower()}",
                event,
                table,
                cast(str, legacy._project(key)),
                normalized=normalized,
            )


def _drop_triggers() -> None:
    names = [
        f"search_{table}_{event}"
        for table in ("paper", "paper_alias", "annotation", "extraction")
        for event in ("insert", "update", "delete")
    ]
    names += ["qa_search_insert", "qa_search_delete"]
    names += [
        f"knowledge_search_{table}_{event}"
        for table in ("concept", "concept_alias", "concept_occurrence")
        for event in ("insert", "delete")
    ]
    for name in names:
        _execute(f"DROP TRIGGER {name}")


def _replace_index(*, normalized: bool) -> None:
    _execute(
        f"CREATE TEMP TABLE {_COPY} AS SELECT rowid AS original_rowid,{_COLUMNS} FROM memory_fts"
    )
    _drop_triggers()
    _execute("DROP TABLE memory_fts")
    indexed = (
        "title UNINDEXED,text UNINDEXED,search_title,search_text" if normalized else "title,text"
    )
    _execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "kind UNINDEXED,id UNINDEXED,paper_id UNINDEXED,page UNINDEXED,"
        f"authorship UNINDEXED,source_version UNINDEXED,{indexed},tokenize='trigram')"
    )
    extra_columns = ",search_title,search_text" if normalized else ""
    extra_values = f",{_FUNCTION}(title),{_FUNCTION}(text)" if normalized else ""
    _execute(
        f"INSERT INTO memory_fts(rowid,{_COLUMNS}{extra_columns}) "
        f"SELECT original_rowid,{_COLUMNS}{extra_values} FROM {_COPY} ORDER BY original_rowid"
    )
    _execute(f"DROP TABLE {_COPY}")
    _fact_triggers(normalized=normalized)
    _memory_triggers(normalized=normalized)
    _execute("INSERT INTO memory_fts(memory_fts) VALUES ('integrity-check')")


def _rebuild(*, normalized: bool) -> None:
    connection = op.get_bind()
    raw = cast(sqlite3.Connection, connection.connection.driver_connection)
    if not raw.in_transaction:
        # pysqlite默认不会为DDL开始物理事务；显式开启，确保整个替换和版本更新可回滚。
        _execute("BEGIN IMMEDIATE")
    with connection.begin_nested():
        _replace_index(normalized=normalized)


def upgrade() -> None:
    _rebuild(normalized=True)


def downgrade() -> None:
    _rebuild(normalized=False)
