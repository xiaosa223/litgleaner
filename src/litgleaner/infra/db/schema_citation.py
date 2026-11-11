"""参考文献原始事实；位置键包含来源与内容指纹，不覆盖旧条目。"""

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


def _array(column: str) -> str:
    return f"CASE WHEN json_valid({column}) THEN json_type({column})='array' ELSE 0 END"


def define_citation(metadata: MetaData) -> Table:
    return Table(
        "citation",
        metadata,
        Column("citing_paper_id", String, ForeignKey("paper.id"), primary_key=True),
        Column("position", String, primary_key=True),
        Column("raw_text", Text, nullable=False),
        Column("source", String, nullable=False),
        Column("source_position", Integer, nullable=False),
        Column("cited_paper_id", String, ForeignKey("paper.id")),
        Column("cited_title", Text),
        Column("cited_aliases", Text, nullable=False),
        Column("contexts", Text, nullable=False),
        Column("intents", Text, nullable=False),
        Column("influential", Boolean),
        Column("created_at", String, nullable=False),
        Column("page", Integer),
        Column("bbox", Text),
        CheckConstraint("length(trim(position)) > 0", name="ck_citation_position"),
        CheckConstraint("length(trim(source)) > 0", name="ck_citation_source"),
        CheckConstraint(
            "typeof(source_position)='integer' AND source_position >= 0",
            name="ck_citation_source_position",
        ),
        CheckConstraint(_array("cited_aliases"), name="ck_citation_aliases"),
        CheckConstraint(_array("contexts"), name="ck_citation_contexts"),
        CheckConstraint(_array("intents"), name="ck_citation_intents"),
        CheckConstraint(
            "influential IS NULL OR influential IN (0,1)", name="ck_citation_influential"
        ),
        CheckConstraint(
            "page IS NULL OR (typeof(page)='integer' AND page>=0)", name="ck_citation_page"
        ),
        CheckConstraint("bbox IS NULL OR (" + _array("bbox") + ")", name="ck_citation_bbox"),
        Index("ix_citation_cited", "cited_paper_id"),
        Index("ix_citation_outgoing", "citing_paper_id", "source", "source_position", "position"),
    )
