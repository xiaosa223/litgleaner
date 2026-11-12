"""采集回执与有序输入索引；内容字节保存在 BlobStore。"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)


def _object(name: str, *, optional: bool = False) -> CheckConstraint:
    condition = f"CASE WHEN json_valid({name}) THEN json_type({name})='object' ELSE 0 END"
    return CheckConstraint(f"{name} IS NULL OR ({condition})" if optional else condition)


def _attempt(metadata: MetaData) -> Table:
    return Table(
        "source_capture_attempt",
        metadata,
        Column("id", String, primary_key=True),
        Column("source", String, nullable=False),
        Column("query", Text, nullable=False),
        Column("query_hash", String, nullable=False),
        Column("cursor", Text),
        Column("purpose", String, nullable=False),
        Column("owner", Text, nullable=False),
        Column("recipe", Text),
        Column("started_at", String, nullable=False),
        Column("started_us", BigInteger, nullable=False),
        Column("status", String, nullable=False),
        Column("finished_at", String),
        Column("error_type", String),
        Column("result", Text),
        *(
            CheckConstraint(f"length(trim({name}))>0")
            for name in ("id", "source", "query_hash", "started_at")
        ),
        CheckConstraint("typeof(started_us)='integer'"),
        CheckConstraint("purpose IN ('discover','inbox','references','survey')"),
        CheckConstraint("status IN ('started','returned','raised')"),
        CheckConstraint("finished_at IS NULL OR length(trim(finished_at))>0"),
        CheckConstraint("error_type IS NULL OR length(trim(error_type))>0"),
        CheckConstraint(
            "(status='started' AND finished_at IS NULL AND error_type IS NULL AND result IS NULL) "
            "OR (status='returned' AND finished_at IS NOT NULL "
            "AND error_type IS NULL AND result IS NOT NULL) "
            "OR (status='raised' AND finished_at IS NOT NULL "
            "AND error_type IS NOT NULL AND result IS NULL)"
        ),
        _object("query"),
        _object("owner"),
        _object("recipe", optional=True),
        _object("result", optional=True),
        Index("ix_source_capture_attempt_source_id", "source", "id"),
        Index("ix_source_capture_attempt_source_started", "source", "started_us"),
    )


def define_capture(metadata: MetaData) -> tuple[Table, Table]:
    attempt = _attempt(metadata)
    inputs = Table(
        "source_capture_input",
        metadata,
        Column(
            "attempt_id",
            String,
            ForeignKey("source_capture_attempt.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("position", Integer, primary_key=True),
        Column("kind", String, nullable=False),
        Column("blob_key", Text, nullable=False),
        Column("sha256", String, nullable=False),
        Column("size", BigInteger, nullable=False),
        CheckConstraint("typeof(position)='integer' AND position>=0"),
        CheckConstraint("typeof(size)='integer' AND size>=0"),
        CheckConstraint("length(trim(kind))>0 AND length(trim(blob_key))>0"),
        CheckConstraint("length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'"),
    )
    return attempt, inputs
