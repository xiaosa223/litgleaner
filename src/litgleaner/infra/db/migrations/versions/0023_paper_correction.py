"""User metadata corrections for papers, append-only (ADR 0011).

Revision ID: 0023
Revises: 0022
"""

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "paper_correction",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("paper_id", sa.String(), sa.ForeignKey("paper.id"), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "field IN ('title','authors','venue','published_at')", name="ck_paper_correction_field"
        ),
        sa.CheckConstraint("length(value) <= 2000", name="ck_paper_correction_value"),
    )
    op.create_index(
        "ix_paper_correction_paper",
        "paper_correction",
        ["paper_id", "field", "created_at", "id"],
    )
    for event in ("UPDATE", "DELETE"):
        op.get_bind().exec_driver_sql(
            f"CREATE TRIGGER paper_correction_no_{event.lower()} BEFORE {event} "
            "ON paper_correction BEGIN SELECT RAISE(ABORT, 'paper_correction_immutable'); END"
        )


def downgrade() -> None:
    for event in ("update", "delete"):
        op.get_bind().exec_driver_sql(f"DROP TRIGGER paper_correction_no_{event}")
    op.drop_index("ix_paper_correction_paper", table_name="paper_correction")
    op.drop_table("paper_correction")
