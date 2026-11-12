"""首次入库及有双侧证据的关系判断表。"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)


def _identity(*names: str) -> tuple[CheckConstraint, ...]:
    return tuple(CheckConstraint(f"length(trim({name}))>0") for name in names)


def define_relations(metadata: MetaData) -> tuple[Table, Table, Table]:
    admission = Table(
        "memory_admission",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("paper_id", String, ForeignKey("paper.id"), nullable=False, unique=True),
        Column("configuration", Text, nullable=False),
        Column("created_at", String, nullable=False),
        Column("baseline", Boolean, nullable=False),
        *_identity("paper_id", "created_at"),
        CheckConstraint("id>0"),
        CheckConstraint("baseline IN (0,1)"),
        CheckConstraint(
            "CASE WHEN json_valid(configuration) THEN json_type(configuration)='object' ELSE 0 END"
        ),
    )
    batch = _batch(metadata)
    relation = _relation(metadata)
    return admission, batch, relation


def _batch(metadata: MetaData) -> Table:
    return Table(
        "relate_batch",
        metadata,
        Column("id", String, primary_key=True),
        Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
        Column("input_hash", String, nullable=False),
        Column("producer_version", String, nullable=False),
        Column("created_at", String, nullable=False),
        Column("data", Text, nullable=False),
        *_identity("id", "paper_id", "input_hash", "producer_version", "created_at"),
        CheckConstraint("CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END"),
        UniqueConstraint("paper_id", "input_hash", "producer_version"),
        Index("ix_relate_batch_paper_created", "paper_id", "created_at", "id"),
    )


def _relation(metadata: MetaData) -> Table:
    return Table(
        "paper_relation",
        metadata,
        Column("id", String, primary_key=True),
        Column("batch_id", String, ForeignKey("relate_batch.id"), nullable=False),
        Column("paper_a", String, ForeignKey("paper.id"), nullable=False),
        Column("paper_b", String, ForeignKey("paper.id"), nullable=False),
        Column("pair_a", String, nullable=False),
        Column("pair_b", String, nullable=False),
        Column("kind", String, nullable=False),
        Column("source", String, nullable=False),
        Column("status", String, nullable=False),
        Column("feedback_id", String, ForeignKey("feedback.id"), unique=True),
        Column("superseded_by", String, ForeignKey("paper_relation.id")),
        Column("created_at", String, nullable=False),
        Column("data", Text, nullable=False),
        *_identity("id", "batch_id", "paper_a", "paper_b", "created_at"),
        CheckConstraint(
            "paper_a<>paper_b AND pair_a=min(paper_a,paper_b) AND pair_b=max(paper_a,paper_b)"
        ),
        CheckConstraint(
            "kind IN ('extends','uses_method_of','compares_with',"
            "'contradicts','similar_approach','same_problem')"
        ),
        CheckConstraint(
            "kind NOT IN ('contradicts','similar_approach','same_problem') OR paper_a<paper_b"
        ),
        CheckConstraint("source IN ('model','user')"),
        CheckConstraint("source<>'user' OR feedback_id IS NOT NULL"),
        CheckConstraint("status IN ('valid','superseded','rejected_by_user')"),
        CheckConstraint(
            "(status='superseded')=(superseded_by IS NOT NULL) AND superseded_by IS NOT id"
        ),
        CheckConstraint("CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END"),
        Index("ix_paper_relation_pair_created", "pair_a", "pair_b", "created_at", "id"),
        Index("ix_paper_relation_a_created", "paper_a", "created_at", "id"),
        Index("ix_paper_relation_b_created", "paper_b", "created_at", "id"),
        Index(
            "uq_relation_model_batch_pair",
            "batch_id",
            "pair_a",
            "pair_b",
            unique=True,
            sqlite_where=Column("source") == "model",
        ),
    )
