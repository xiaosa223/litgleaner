"""inbox: user-added papers

Revision ID: 0007
Revises: 0006
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbox",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("pdf_sha256", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("paper_id", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("inbox", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_inbox_status"), ["status"], unique=False)
        batch_op.create_index(batch_op.f("ix_inbox_paper_id"), ["paper_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("inbox", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_inbox_paper_id"))
        batch_op.drop_index(batch_op.f("ix_inbox_status"))
    op.drop_table("inbox")
