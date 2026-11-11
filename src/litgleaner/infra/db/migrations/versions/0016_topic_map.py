"""Append-only topic maps keyed by input and producer.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

_IDENTITY = ("id", "topic_id", "topic_hash", "input_hash", "producer_version", "created_at")


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _execute(
        "CREATE TABLE topic_map (id VARCHAR PRIMARY KEY NOT NULL,topic_id VARCHAR NOT NULL,"
        "topic_hash VARCHAR NOT NULL,input_hash VARCHAR NOT NULL,producer_version VARCHAR NOT NULL,"
        "created_at VARCHAR NOT NULL,data TEXT NOT NULL,"
        "UNIQUE(topic_id,topic_hash,input_hash,producer_version),"
        "CHECK(CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END),"
        + ",".join(f"CHECK(length(trim({name}))>0)" for name in _IDENTITY)
        + ")"
    )
    _execute("CREATE INDEX ix_topic_map_topic_created ON topic_map(topic_id,created_at,id)")
    for event in ("UPDATE", "DELETE"):
        _execute(
            f"CREATE TRIGGER topic_map_no_{event.lower()} BEFORE {event} ON topic_map "
            "BEGIN SELECT RAISE(ABORT, 'topic_map_immutable'); END"
        )
    invalid = " OR ".join(
        f"json_extract(new.data,'$.{name}') IS NOT new.{name}" for name in _IDENTITY
    )
    _execute(
        "CREATE TRIGGER topic_map_json BEFORE INSERT ON topic_map WHEN "
        f"CASE WHEN json_valid(new.data) THEN ({invalid}) ELSE 0 END "
        "BEGIN SELECT RAISE(ABORT, 'topic_map_json_invalid'); END"
    )


def downgrade() -> None:
    for name in ("topic_map_json", "topic_map_no_delete", "topic_map_no_update"):
        _execute(f"DROP TRIGGER {name}")
    op.drop_index("ix_topic_map_topic_created", table_name="topic_map")
    op.drop_table("topic_map")
