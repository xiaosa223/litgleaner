"""Concept relations, duplicate candidates and user decisions (append-only).

Revision ID: 0024
Revises: 0023
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

_TABLES = ("concept_decision", "concept_merge_candidate", "concept_link")


def _pair() -> list[sa.Column[str]]:
    return [
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("concept_a", sa.String(), sa.ForeignKey("concept.id"), nullable=False),
        sa.Column("concept_b", sa.String(), sa.ForeignKey("concept.id"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "concept_link",
        *_pair(),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("guide_id", sa.String(), sa.ForeignKey("explanation.id"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('prerequisite','similar','contrast')", name="ck_concept_link_kind"
        ),
        sa.CheckConstraint("concept_a <> concept_b", name="ck_concept_link_pair"),
        sa.UniqueConstraint("concept_a", "concept_b", "kind"),
    )
    op.create_table(
        "concept_merge_candidate",
        *_pair(),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("guide_id", sa.String(), sa.ForeignKey("explanation.id"), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.CheckConstraint("concept_a <> concept_b", name="ck_concept_merge_pair"),
        sa.UniqueConstraint("concept_a", "concept_b"),
    )
    op.create_table(
        "concept_decision",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("target", sa.String(), nullable=False),
        sa.Column("decision", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.CheckConstraint("decision IN ('confirmed','rejected')", name="ck_concept_decision"),
    )
    op.create_index(
        "ix_concept_decision_target", "concept_decision", ["target", "created_at", "id"]
    )
    for table in _TABLES:
        for event in ("UPDATE", "DELETE"):
            op.get_bind().exec_driver_sql(
                f"CREATE TRIGGER {table}_no_{event.lower()} BEFORE {event} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{table}_immutable'); END"
            )


def downgrade() -> None:
    for table in _TABLES:
        for event in ("update", "delete"):
            op.get_bind().exec_driver_sql(f"DROP TRIGGER {table}_no_{event}")
    op.drop_index("ix_concept_decision_target", table_name="concept_decision")
    for table in _TABLES:
        op.drop_table(table)
