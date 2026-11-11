"""Local full-text and personal memory index, rebuilt from canonical records.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_COLUMNS = "kind,id,paper_id,page,authorship,source_version,title,text"


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def _paper(where: str) -> str:
    return (
        f"INSERT INTO memory_fts({_COLUMNS}) "
        "SELECT 'paper',p.id,p.id,NULL,'author','',p.title,"
        "p.authors || ' ' || coalesce(p.venue,'') || ' ' || coalesce(p.abstract,'') || ' ' || "
        "coalesce((SELECT group_concat(value,' ') FROM paper_alias WHERE paper_id=p.id),'') "
        f"FROM paper p WHERE {where};"
    )


def _annotation(where: str) -> str:
    return (
        f"INSERT INTO memory_fts({_COLUMNS}) "
        "SELECT a.kind,a.id || ':body',a.paper_id,a.page,'user','','',a.body "
        f"FROM annotation a WHERE a.deleted_at IS NULL AND coalesce(a.body,'') != '' AND {where};"
        f"INSERT INTO memory_fts({_COLUMNS}) "
        "SELECT 'highlight',a.id || ':quote',a.paper_id,a.page,'author','','',a.quote "
        f"FROM annotation a WHERE a.deleted_at IS NULL AND coalesce(a.quote,'') != '' AND {where};"
    )


def _extraction(where: str) -> str:
    return (
        f"INSERT INTO memory_fts({_COLUMNS}) "
        "SELECT 'extraction',cast(e.id AS TEXT),e.paper_id,NULL,'model','','',"
        "coalesce(json_extract(e.data,'$.summary'),'') || ' ' || "
        "coalesce(json_extract(e.data,'$.problem'),'') || ' ' || "
        "coalesce(json_extract(e.data,'$.approach'),'') "
        f"FROM extraction e WHERE {where} AND NOT EXISTS "
        "(SELECT 1 FROM extraction newer WHERE newer.paper_id=e.paper_id AND newer.id>e.id);"
    )


def _trigger(name: str, event: str, table: str, body: str) -> None:
    # SQLAlchemy exec_driver_sql 执行单条 SQL；触发器体内允许多条语句。
    _execute(f"CREATE TRIGGER {name} AFTER {event} ON {table} BEGIN {body} END")


def upgrade() -> None:
    _execute(
        "CREATE VIRTUAL TABLE memory_fts USING fts5("
        "kind UNINDEXED,id UNINDEXED,paper_id UNINDEXED,page UNINDEXED,"
        "authorship UNINDEXED,source_version UNINDEXED,title,text,tokenize='trigram')"
    )
    for table in ("paper", "paper_alias"):
        for event in ("INSERT", "UPDATE", "DELETE"):
            alias = "old" if event == "DELETE" else "new"
            key = f"{alias}.id" if table == "paper" else f"{alias}.paper_id"
            body = f"DELETE FROM memory_fts WHERE kind='paper' AND paper_id={key};"
            if table != "paper" or event != "DELETE":
                body += _paper(f"p.id={key}")
            _trigger(f"search_{table}_{event.lower()}", event, table, body)
    for event in ("INSERT", "UPDATE", "DELETE"):
        alias = "old" if event == "DELETE" else "new"
        body = (
            f"DELETE FROM memory_fts WHERE id IN ({alias}.id || ':body', {alias}.id || ':quote') "
            "AND kind IN ('note','highlight');"
        )
        if event != "DELETE":
            body += _annotation("a.id=new.id")
        _trigger(f"search_annotation_{event.lower()}", event, "annotation", body)
        body = f"DELETE FROM memory_fts WHERE kind='extraction' AND paper_id={alias}.paper_id;"
        body += _extraction(f"e.paper_id={alias}.paper_id")
        _trigger(f"search_extraction_{event.lower()}", event, "extraction", body)
    _execute(_paper("1"))
    for statement in _annotation("1").split(";"):
        if statement:
            _execute(statement)
    _execute(_extraction("1"))


def downgrade() -> None:
    for table in ("paper", "paper_alias", "annotation", "extraction"):
        for event in ("insert", "update", "delete"):
            _execute(f"DROP TRIGGER search_{table}_{event}")
    _execute("DROP TABLE memory_fts")
