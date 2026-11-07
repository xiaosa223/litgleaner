"""reading state and annotation

Revision ID: 0005
Revises: 0004
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "reading_state",
        sa.Column("paper_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("position", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"]),
        sa.PrimaryKeyConstraint("paper_id"),
    )
    with op.batch_alter_table("reading_state", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_reading_state_status"), ["status"], unique=False)
    op.create_table(
        "annotation",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("paper_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("bbox", sa.Text(), nullable=True),
        sa.Column("quote", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("rects", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("deleted_at", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["paper.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("annotation", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_annotation_paper_id"), ["paper_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("annotation", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_annotation_paper_id"))
    op.drop_table("annotation")
    with op.batch_alter_table("reading_state", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_reading_state_status"))
    op.drop_table("reading_state")
