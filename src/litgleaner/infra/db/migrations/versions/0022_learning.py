"""Personal guidance cache, answer cards, evaluations and mastery events (append-only).

Revision ID: 0022
Revises: 0021
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_JSON = "CASE WHEN json_valid(data) THEN json_type(data)='object' ELSE 0 END"
_TABLES = ("mastery_event", "answer_evaluation", "answer_card", "explanation")


def _immutable(table: str) -> None:
    for event in ("UPDATE", "DELETE"):
        op.get_bind().exec_driver_sql(
            f"CREATE TRIGGER {table}_no_{event.lower()} BEFORE {event} ON {table} "
            f"BEGIN SELECT RAISE(ABORT, '{table}_immutable'); END"
        )


def upgrade() -> None:
    op.create_table(
        "explanation",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("paper_id", sa.String(), sa.ForeignKey("paper.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("producer_version", sa.String(), nullable=False),
        sa.Column("reading_language", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.CheckConstraint("kind IN ('guide')", name="ck_explanation_kind"),
        sa.CheckConstraint(_JSON, name="ck_explanation_data"),
        sa.UniqueConstraint("paper_id", "kind", "fingerprint", "producer_version"),
    )
    op.create_index("ix_explanation_paper", "explanation", ["paper_id", "kind", "created_at", "id"])
    op.create_table(
        "answer_card",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("paper_id", sa.String(), sa.ForeignKey("paper.id"), nullable=False),
        sa.Column("explanation_id", sa.String(), sa.ForeignKey("explanation.id"), nullable=False),
        sa.Column("question_index", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.CheckConstraint("question_index BETWEEN 0 AND 2", name="ck_answer_card_question"),
        sa.CheckConstraint("revision >= 1", name="ck_answer_card_revision"),
        sa.CheckConstraint(
            "length(trim(text)) > 0 AND length(text) <= 2000", name="ck_answer_card_text"
        ),
        sa.UniqueConstraint("explanation_id", "question_index", "revision"),
    )
    op.create_index("ix_answer_card_paper", "answer_card", ["paper_id", "created_at"])
    op.create_table(
        "answer_evaluation",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "card_id", sa.String(), sa.ForeignKey("answer_card.id"), nullable=False, unique=True
        ),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("prompt_version", sa.String(), nullable=False),
        sa.Column("input_hash", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.CheckConstraint(_JSON, name="ck_answer_evaluation_data"),
    )
    op.create_table(
        "mastery_event",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("concept_id", sa.String(), sa.ForeignKey("concept.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("level", sa.Integer()),
        sa.Column("rating", sa.String()),
        sa.Column("question_kind", sa.String()),
        sa.Column("verdict", sa.String()),
        sa.Column("overall", sa.String()),
        sa.Column("source_id", sa.String()),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.CheckConstraint(
            "(kind='override' AND level BETWEEN 0 AND 3 AND rating IS NULL AND verdict IS NULL "
            "AND question_kind IS NULL AND overall IS NULL) "
            "OR (kind='evaluation' AND level IS NULL AND rating IS NULL "
            "AND question_kind IN ('understand','compare','transfer') "
            "AND verdict IN ('correct','missing','misunderstood') "
            "AND overall IN ('correct','partial','incorrect')) "
            "OR (kind='review' AND level IS NULL AND verdict IS NULL "
            "AND rating IN ('remembered','fuzzy','forgot'))",
            name="ck_mastery_event_kind",
        ),
    )
    op.create_index("ix_mastery_event_concept", "mastery_event", ["concept_id", "created_at", "id"])
    for table in _TABLES:
        _immutable(table)


def downgrade() -> None:
    for table in _TABLES:
        for event in ("update", "delete"):
            op.get_bind().exec_driver_sql(f"DROP TRIGGER {table}_no_{event}")
    op.drop_index("ix_mastery_event_concept", table_name="mastery_event")
    op.drop_table("mastery_event")
    op.drop_table("answer_evaluation")
    op.drop_index("ix_answer_card_paper", table_name="answer_card")
    op.drop_table("answer_card")
    op.drop_index("ix_explanation_paper", table_name="explanation")
    op.drop_table("explanation")
