"""Source-reported publication types and citation counts without rebuilding paper.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def upgrade() -> None:
    _execute(
        "ALTER TABLE paper ADD COLUMN publication_types TEXT "
        "CONSTRAINT ck_paper_publication_types CHECK (publication_types IS NULL OR "
        "CASE WHEN json_valid(publication_types) THEN json_type(publication_types)='array' "
        "ELSE 0 END)"
    )
    _execute(
        "ALTER TABLE paper ADD COLUMN citation_count INTEGER "
        "CONSTRAINT ck_paper_citation_count CHECK (citation_count IS NULL OR "
        "(typeof(citation_count)='integer' AND citation_count>=0))"
    )
    for event in ("INSERT", "UPDATE OF publication_types"):
        suffix = "insert" if event == "INSERT" else "update"
        _execute(
            f"CREATE TRIGGER paper_types_{suffix} BEFORE {event} ON paper "
            "WHEN new.publication_types IS NOT NULL AND "
            "CASE WHEN json_valid(new.publication_types) THEN EXISTS "
            "(SELECT 1 FROM json_each(new.publication_types) AS item "
            "WHERE item.type!='text' OR length(trim(item.value))=0) ELSE 0 END "
            "BEGIN SELECT RAISE(ABORT, 'paper_publication_types_invalid'); END"
        )


def downgrade() -> None:
    for event in ("insert", "update"):
        _execute(f"DROP TRIGGER paper_types_{event}")
    _execute("ALTER TABLE paper DROP COLUMN citation_count")
    _execute("ALTER TABLE paper DROP COLUMN publication_types")
