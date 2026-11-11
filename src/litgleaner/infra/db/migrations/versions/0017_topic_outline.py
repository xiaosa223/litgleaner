"""Append-only frozen topic outlines.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

_IDENTITY = ("id", "topic_id", "topic_hash", "input_hash", "producer_version", "created_at")


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _execute(
        "CREATE TABLE topic_outline (id VARCHAR PRIMARY KEY NOT NULL,topic_id VARCHAR NOT NULL,"
        "topic_hash VARCHAR NOT NULL,input_hash VARCHAR NOT NULL,producer_version VARCHAR NOT NULL,"
        "created_at VARCHAR NOT NULL,data TEXT NOT NULL,"
        "UNIQUE(topic_id,topic_hash,input_hash,producer_version),"
        "CHECK(CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END),"
        + ",".join(f"CHECK(length(trim({name}))>0)" for name in _IDENTITY)
        + ")"
    )
    _execute("CREATE INDEX ix_topic_outline_topic_created ON topic_outline(topic_id,created_at,id)")
    for event in ("UPDATE", "DELETE"):
        _execute(
            f"CREATE TRIGGER topic_outline_no_{event.lower()} BEFORE {event} ON topic_outline "
            "BEGIN SELECT RAISE(ABORT, 'topic_outline_immutable'); END"
        )
    invalid = " OR ".join(
        f"json_type(new.data,'$.{name}') IS NOT 'text' "
        f"OR json_extract(new.data,'$.{name}') IS NOT new.{name}"
        for name in _IDENTITY
    )
    _execute(
        "CREATE TRIGGER topic_outline_json BEFORE INSERT ON topic_outline WHEN "
        f"CASE WHEN json_valid(new.data) THEN ({invalid}) ELSE 0 END "
        "BEGIN SELECT RAISE(ABORT, 'topic_outline_json_invalid'); END"
    )
    _execute(
        "CREATE TRIGGER topic_outline_no_replace BEFORE INSERT ON topic_outline WHEN EXISTS("
        "SELECT 1 FROM topic_outline WHERE id=new.id OR (topic_id=new.topic_id AND "
        "topic_hash=new.topic_hash AND input_hash=new.input_hash AND "
        "producer_version=new.producer_version)) "
        "BEGIN SELECT RAISE(ABORT, 'topic_outline_immutable'); END"
    )


def downgrade() -> None:
    for name in ("json", "no_replace", "no_delete", "no_update"):
        _execute(f"DROP TRIGGER topic_outline_{name}")
    op.drop_index("ix_topic_outline_topic_created", table_name="topic_outline")
    op.drop_table("topic_outline")
