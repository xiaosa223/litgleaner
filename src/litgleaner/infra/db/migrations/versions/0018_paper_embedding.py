"""Append-only local float32 embeddings.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

_IDENTITY = (
    "paper_id",
    "input_hash",
    "embedder_version",
    "configuration_version",
    "model",
    "input_text",
    "created_at",
)


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _execute(
        "CREATE TABLE paper_embedding (paper_id VARCHAR NOT NULL REFERENCES paper(id),"
        "input_hash VARCHAR NOT NULL,embedder_version VARCHAR NOT NULL,"
        "configuration_version VARCHAR NOT NULL,model VARCHAR NOT NULL,"
        "dimensions INTEGER NOT NULL,input_text TEXT NOT NULL,vector BLOB NOT NULL,"
        "source_extraction_id INTEGER REFERENCES extraction(id),created_at VARCHAR NOT NULL,"
        "PRIMARY KEY(paper_id,input_hash,embedder_version),"
        "CHECK(typeof(dimensions)='integer' AND dimensions BETWEEN 1 AND 65536),"
        "CHECK(typeof(vector)='blob' AND length(vector)=4*dimensions),"
        + ",".join(f"CHECK(length(trim({name}))>0)" for name in _IDENTITY)
        + ")"
    )
    _execute(
        "CREATE INDEX ix_paper_embedding_paper_created ON paper_embedding(paper_id,created_at)"
    )
    _execute(
        "CREATE INDEX ix_paper_embedding_source_extraction_id "
        "ON paper_embedding(source_extraction_id)"
    )
    for event in ("UPDATE", "DELETE"):
        _execute(
            f"CREATE TRIGGER paper_embedding_no_{event.lower()} BEFORE {event} ON paper_embedding "
            "BEGIN SELECT RAISE(ABORT, 'paper_embedding_immutable'); END"
        )
    _execute(
        "CREATE TRIGGER paper_embedding_no_replace BEFORE INSERT ON paper_embedding WHEN EXISTS("
        "SELECT 1 FROM paper_embedding WHERE (paper_id=new.paper_id AND input_hash=new.input_hash "
        "AND embedder_version=new.embedder_version) OR rowid=new.rowid) "
        "BEGIN SELECT RAISE(ABORT, 'paper_embedding_immutable'); END"
    )
    _execute(
        "CREATE TRIGGER paper_embedding_source BEFORE INSERT ON paper_embedding WHEN "
        "new.source_extraction_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM extraction WHERE "
        "id=new.source_extraction_id AND paper_id=new.paper_id) "
        "BEGIN SELECT RAISE(ABORT, 'paper_embedding_source_invalid'); END"
    )
    _execute(
        "CREATE TRIGGER paper_embedding_rowid AFTER INSERT ON paper_embedding WHEN new.rowid<=0 "
        "BEGIN SELECT RAISE(ABORT, 'paper_embedding_rowid_invalid'); END"
    )


def downgrade() -> None:
    for name in ("rowid", "source", "no_replace", "no_delete", "no_update"):
        _execute(f"DROP TRIGGER paper_embedding_{name}")
    op.drop_index("ix_paper_embedding_source_extraction_id", table_name="paper_embedding")
    op.drop_index("ix_paper_embedding_paper_created", table_name="paper_embedding")
    op.drop_table("paper_embedding")
