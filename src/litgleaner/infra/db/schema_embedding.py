"""本地向量按输入和生产者联合指纹只追加。"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
)


def define_embedding(metadata: MetaData) -> Table:
    return Table(
        "paper_embedding",
        metadata,
        Column("paper_id", String, ForeignKey("paper.id"), primary_key=True),
        Column("input_hash", String, primary_key=True),
        Column("embedder_version", String, primary_key=True),
        Column("configuration_version", String, nullable=False),
        Column("model", String, nullable=False),
        Column("dimensions", Integer, nullable=False),
        Column("input_text", Text, nullable=False),
        Column("vector", LargeBinary, nullable=False),
        Column("source_extraction_id", Integer, ForeignKey("extraction.id")),
        Column("created_at", String, nullable=False),
        *(
            CheckConstraint(f"length(trim({name}))>0")
            for name in (
                "paper_id",
                "input_hash",
                "embedder_version",
                "configuration_version",
                "model",
                "input_text",
                "created_at",
            )
        ),
        CheckConstraint("typeof(dimensions)='integer' AND dimensions BETWEEN 1 AND 65536"),
        CheckConstraint("typeof(vector)='blob' AND length(vector)=4*dimensions"),
        Index("ix_paper_embedding_paper_created", "paper_id", "created_at"),
        Index("ix_paper_embedding_source_extraction_id", "source_extraction_id"),
    )
