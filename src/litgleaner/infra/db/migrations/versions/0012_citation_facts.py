"""Immutable source-scoped reference entries with optional local paper links.

Revision ID: 0012
Revises: 0011
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _array(column: str) -> str:
    return f"CASE WHEN json_valid({column}) THEN json_type({column})='array' ELSE 0 END"


def _create_table() -> None:
    op.create_table(
        "citation",
        sa.Column("citing_paper_id", sa.String, sa.ForeignKey("paper.id"), primary_key=True),
        sa.Column("position", sa.String, primary_key=True),
        sa.Column("raw_text", sa.Text, nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("source_position", sa.Integer, nullable=False),
        sa.Column("cited_paper_id", sa.String, sa.ForeignKey("paper.id")),
        sa.Column("cited_title", sa.Text),
        sa.Column("cited_aliases", sa.Text, nullable=False),
        sa.Column("contexts", sa.Text, nullable=False),
        sa.Column("intents", sa.Text, nullable=False),
        sa.Column("influential", sa.Boolean),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("page", sa.Integer),
        sa.Column("bbox", sa.Text),
        sa.CheckConstraint("length(trim(position)) > 0", name="ck_citation_position"),
        sa.CheckConstraint("length(trim(source)) > 0", name="ck_citation_source"),
        sa.CheckConstraint(
            "typeof(source_position)='integer' AND source_position >= 0",
            name="ck_citation_source_position",
        ),
        sa.CheckConstraint(_array("cited_aliases"), name="ck_citation_aliases"),
        sa.CheckConstraint(_array("contexts"), name="ck_citation_contexts"),
        sa.CheckConstraint(_array("intents"), name="ck_citation_intents"),
        sa.CheckConstraint(
            "influential IS NULL OR influential IN (0,1)", name="ck_citation_influential"
        ),
        sa.CheckConstraint(
            "page IS NULL OR (typeof(page)='integer' AND page>=0)", name="ck_citation_page"
        ),
        sa.CheckConstraint("bbox IS NULL OR (" + _array("bbox") + ")", name="ck_citation_bbox"),
    )


def _json_triggers() -> None:
    predicates = {
        "contexts": "item.type!='text'",
        "intents": "item.type!='text'",
        "cited_aliases": (
            "CASE WHEN item.type='object' THEN "
            "coalesce(json_type(item.value,'$.scheme'),'')!='text' OR "
            "coalesce(json_type(item.value,'$.value'),'')!='text' ELSE 1 END"
        ),
    }
    for column, predicate in predicates.items():
        op.get_bind().exec_driver_sql(
            f"CREATE TRIGGER citation_json_{column} BEFORE INSERT ON citation "
            f"WHEN CASE WHEN json_valid(new.{column}) THEN EXISTS "
            f"(SELECT 1 FROM json_each(new.{column}) AS item WHERE {predicate}) ELSE 0 END "
            "BEGIN SELECT RAISE(ABORT, 'citation_json_invalid'); END"
        )


def upgrade() -> None:
    _create_table()
    _json_triggers()
    op.create_index("ix_citation_cited", "citation", ["cited_paper_id"])
    op.create_index(
        "ix_citation_outgoing",
        "citation",
        ["citing_paper_id", "source", "source_position", "position"],
    )
    op.get_bind().exec_driver_sql(
        "CREATE TRIGGER citation_immutable BEFORE UPDATE ON citation "
        "BEGIN SELECT RAISE(ABORT, 'citation_fact_immutable'); END"
    )


def downgrade() -> None:
    op.get_bind().exec_driver_sql("DROP TRIGGER citation_immutable")
    for column in ("contexts", "intents", "cited_aliases"):
        op.get_bind().exec_driver_sql(f"DROP TRIGGER citation_json_{column}")
    op.drop_index("ix_citation_outgoing", table_name="citation")
    op.drop_index("ix_citation_cited", table_name="citation")
    op.drop_table("citation")
