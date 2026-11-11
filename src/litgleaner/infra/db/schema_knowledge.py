"""全局概念、精确别名和论文位置；仅包含事实，不存掌握程度。"""

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
)


def _concept(metadata: MetaData) -> Table:
    return Table(
        "concept",
        metadata,
        Column("id", String, primary_key=True),
        Column("name", Text, nullable=False),
        Column("created_at", String, nullable=False),
        CheckConstraint("length(trim(name)) > 0", name="ck_concept_name"),
    )


def _alias(metadata: MetaData) -> Table:
    return Table(
        "concept_alias",
        metadata,
        Column("normalized_name", String, primary_key=True),
        Column("concept_id", String, ForeignKey("concept.id"), nullable=False),
        Column("name", Text, nullable=False),
        Column("language", String),
        Column("created_at", String, nullable=False),
        CheckConstraint("length(trim(normalized_name)) > 0", name="ck_concept_alias_key"),
        CheckConstraint("length(trim(name)) > 0", name="ck_concept_alias_name"),
        Index("ix_concept_alias_concept", "concept_id"),
    )


def _occurrence(metadata: MetaData) -> Table:
    return Table(
        "concept_occurrence",
        metadata,
        Column("concept_id", String, ForeignKey("concept.id"), primary_key=True),
        Column("paper_id", String, ForeignKey("paper.id"), primary_key=True),
        Column("role", String, nullable=False),
        Column("definition", Text),
        Column("page", Integer, nullable=False),
        Column("bbox", Text),
        Column("verified", Boolean, nullable=False),
        Column("input_hash", String, nullable=False),
        Column("producer_version", String, nullable=False),
        Column("created_at", String, nullable=False),
        CheckConstraint("role IN ('core','prerequisite','mentioned')", name="ck_concept_role"),
        CheckConstraint("typeof(page) = 'integer' AND page >= 0", name="ck_concept_page"),
        CheckConstraint("verified IN (0,1)", name="ck_concept_verified"),
        CheckConstraint(
            "(verified=1 AND definition IS NOT NULL AND length(trim(definition))>=10) "
            "OR (verified=0 AND definition IS NULL)",
            name="ck_concept_definition",
        ),
        CheckConstraint("bbox IS NULL OR json_valid(bbox)", name="ck_concept_bbox"),
        Index("ix_concept_occurrence_paper", "paper_id"),
    )


def define_knowledge(metadata: MetaData) -> tuple[Table, Table, Table]:
    return _concept(metadata), _alias(metadata), _occurrence(metadata)
