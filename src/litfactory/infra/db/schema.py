"""数据库结构。修改后必须生成 Alembic 迁移，测试会检查两者一致。"""

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

paper = Table(
    "paper",
    metadata,
    Column("id", String, primary_key=True),
    Column("title", Text, nullable=False),
    Column("title_key", Text, nullable=False, index=True),
    Column("abstract", Text),
    Column("authors", Text, nullable=False),
    Column("venue", Text),
    Column("issn", String),
    Column("published_at", String),
    Column("first_seen_at", String, nullable=False, index=True),
)

paper_alias = Table(
    "paper_alias",
    metadata,
    Column("scheme", String, primary_key=True),
    Column("value", String, primary_key=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False, index=True),
)

merge_log = Table(
    "merge_log",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
    Column("kind", String, nullable=False),
    Column("status", String, nullable=False),
    Column("detail", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

sighting = Table(
    "sighting",
    metadata,
    Column("paper_id", String, ForeignKey("paper.id"), primary_key=True),
    Column("source", String, primary_key=True),
    Column("query_hash", String, primary_key=True),
    Column("raw_key", String),
    Column("first_seen_at", String, nullable=False),
    Column("last_seen_at", String, nullable=False),
)

source_cursor = Table(
    "source_cursor",
    metadata,
    Column("source", String, primary_key=True),
    Column("query_hash", String, primary_key=True),
    Column("cursor", Text),
    Column("updated_at", String, nullable=False),
)

artifact = Table(
    "artifact",
    metadata,
    Column("sha256", String, primary_key=True),
    Column("kind", String, primary_key=True),
    Column("blob_key", String, nullable=False),
    Column("size", Integer, nullable=False),
    Column("producer", String, nullable=False),
    Column("license_basis", String),
    Column("pinned", Boolean, nullable=False, default=False),
    Column("expires_at", String),
    Column("created_at", String, nullable=False),
)

fulltext = Table(
    "fulltext",
    metadata,
    Column("paper_id", String, ForeignKey("paper.id"), primary_key=True),
    Column("status", String, nullable=False, index=True),
    Column("pdf_sha256", String),
    Column("doc_key", String),
    Column("provider", String),
    Column("license_basis", String),
    Column("quality", String),
    Column("updated_at", String, nullable=False),
)

extraction = Table(
    "extraction",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
    Column("input_hash", String, nullable=False),
    Column("extractor_version", String, nullable=False),
    Column("data", Text, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("paper_id", "input_hash", "extractor_version"),
)

topic_state = Table(
    "topic_state",
    metadata,
    Column("topic_id", String, primary_key=True),
    Column("status", String, nullable=False),
    Column("topic_hash", String, nullable=False),
    Column("triage_from", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

triage = Table(
    "triage",
    metadata,
    Column("paper_id", String, ForeignKey("paper.id"), primary_key=True),
    Column("topic_id", String, primary_key=True),
    Column("topic_hash", String, primary_key=True),
    Column("relevant", Boolean, nullable=False),
    Column("reason", Text, nullable=False),
    Column("prompt_version", String, nullable=False),
    Column("model", String, nullable=False),
    Column("created_at", String, nullable=False),
)

assessment = Table(
    "assessment",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False),
    Column("topic_id", String, nullable=False),
    Column("topic_hash", String, nullable=False),
    Column("semantic_hash", String, nullable=False),
    Column("input_hash", String, nullable=False),
    Column("levels", Text, nullable=False),
    Column("evidence", Text, nullable=False),
    Column("fulltext_used", Boolean, nullable=False),
    Column("model", String, nullable=False),
    Column("prompt_version", String, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("paper_id", "topic_id", "topic_hash", "semantic_hash", "input_hash"),
    Index("ix_assessment_topic", "topic_id", "topic_hash", "semantic_hash"),
)

score = Table(
    "score",
    metadata,
    Column("assessment_id", Integer, ForeignKey("assessment.id"), primary_key=True),
    Column("policy_hash", String, primary_key=True),
    Column("score", Integer, nullable=False),
    Column("tier", String, nullable=False),
    Column("provisional", Boolean, nullable=False),
    Column("created_at", String, nullable=False),
)

rubric_snapshot = Table(
    "rubric_snapshot",
    metadata,
    Column("hash", String, primary_key=True),
    Column("rubric_id", String, nullable=False),
    Column("body", Text, nullable=False),
    Column("created_at", String, nullable=False),
)

digest = Table(
    "digest",
    metadata,
    Column("id", String, primary_key=True),
    Column("route_id", String, nullable=False),
    Column("topic_id", String, nullable=False),
    Column("channel", String, nullable=False),
    Column("period", String, nullable=False),
    Column("created_at", String, nullable=False),
    UniqueConstraint("route_id", "topic_id", "period"),
)

digest_item = Table(
    "digest_item",
    metadata,
    Column("digest_id", String, ForeignKey("digest.id"), primary_key=True),
    Column("position", Integer, primary_key=True),
    Column("paper_id", String, ForeignKey("paper.id"), nullable=False, index=True),
    Column("card", Text, nullable=False),
)

delivery = Table(
    "delivery",
    metadata,
    Column("digest_id", String, ForeignKey("digest.id"), primary_key=True),
    Column("channel", String, primary_key=True),
    Column("status", String, nullable=False, index=True),
    Column("external_id", String),
    Column("last_error", Text),
    Column("updated_at", String, nullable=False),
)

job = Table(
    "job",
    metadata,
    Column("key", String, primary_key=True),
    Column("stage", String, nullable=False),
    Column("payload", Text, nullable=False),
    Column("status", String, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("next_run_at", String, nullable=False),
    Column("last_error", Text),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    Index("ix_job_due", "stage", "status", "next_run_at"),
)

llm_call = Table(
    "llm_call",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task", String, nullable=False),
    Column("model", String, nullable=False),
    Column("prompt_version", String, nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Float, nullable=False),
    Column("ok", Boolean, nullable=False),
    Column("created_at", String, nullable=False, index=True),
)
