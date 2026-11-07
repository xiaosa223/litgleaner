"""translation cache, llm_call.paper_id

Revision ID: 0006
Revises: 0005
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("llm_call", schema=None) as batch_op:
        batch_op.add_column(sa.Column("paper_id", sa.String(), nullable=True))
        batch_op.create_index(batch_op.f("ix_llm_call_paper_id"), ["paper_id"], unique=False)
    op.create_table(
        "translation",
        sa.Column("unit", sa.String(), nullable=False),
        sa.Column("source_hash", sa.String(), nullable=False),
        sa.Column("target_lang", sa.String(), nullable=False),
        sa.Column("glossary_hash", sa.String(), nullable=False),
        sa.Column("translator_version", sa.String(), nullable=False),
        sa.Column("sentences", sa.Text(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("paper_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint(
            "unit", "source_hash", "target_lang", "glossary_hash", "translator_version"
        ),
    )
    with op.batch_alter_table("translation", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_translation_paper_id"), ["paper_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("translation", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_translation_paper_id"))
    op.drop_table("translation")
    with op.batch_alter_table("llm_call", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_llm_call_paper_id"))
        batch_op.drop_column("paper_id")
