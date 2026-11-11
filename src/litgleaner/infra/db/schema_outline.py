"""综述骨架的冻结结果，仅按指纹追加。"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    Index,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)


def define_outline(metadata: MetaData) -> Table:
    return Table(
        "topic_outline",
        metadata,
        Column("id", String, primary_key=True),
        Column("topic_id", String, nullable=False),
        Column("topic_hash", String, nullable=False),
        Column("input_hash", String, nullable=False),
        Column("producer_version", String, nullable=False),
        Column("created_at", String, nullable=False),
        Column("data", Text, nullable=False),
        *(
            CheckConstraint(f"length(trim({name}))>0")
            for name in (
                "id",
                "topic_id",
                "topic_hash",
                "input_hash",
                "producer_version",
                "created_at",
            )
        ),
        CheckConstraint("CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END"),
        UniqueConstraint("topic_id", "topic_hash", "input_hash", "producer_version"),
        Index("ix_topic_outline_topic_created", "topic_id", "created_at", "id"),
    )
