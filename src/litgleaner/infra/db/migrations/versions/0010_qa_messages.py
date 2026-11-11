"""Append-only paper questions and cited answers with a searchable projection.

Revision ID: 0010
Revises: 0009
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def _create_table() -> None:
    op.create_table(
        "qa_message",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("paper_id", sa.String, sa.ForeignKey("paper.id"), nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("parent_id", sa.String, sa.ForeignKey("qa_message.id")),
        sa.Column("request_id", sa.String),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("page", sa.Integer),
        sa.Column("data", sa.Text, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
        sa.UniqueConstraint("paper_id", "request_id", name="uq_qa_question_request"),
        sa.UniqueConstraint("parent_id", name="uq_qa_answer_parent"),
        sa.CheckConstraint(
            "(role = 'question' AND parent_id IS NULL AND request_id IS NOT NULL "
            "AND length(trim(request_id)) > 0) OR "
            "(role = 'answer' AND parent_id IS NOT NULL AND request_id IS NULL)",
            name="ck_qa_role_fields",
        ),
        sa.CheckConstraint("length(trim(text)) > 0", name="ck_qa_text"),
        sa.CheckConstraint(
            "page IS NULL OR (typeof(page) = 'integer' AND page >= 0)", name="ck_qa_page"
        ),
        sa.CheckConstraint("json_valid(data)", name="ck_qa_data"),
    )
    op.create_index("ix_qa_paper_created", "qa_message", ["paper_id", "created_at", "id"])


def _link_annotations() -> None:
    # 直接追加 nullable FK 列，保留 annotation 及原有 12 个 FTS 触发器。
    _execute("ALTER TABLE annotation ADD COLUMN qa_question_id VARCHAR REFERENCES qa_message(id)")
    for event in ("INSERT", "UPDATE OF qa_question_id,paper_id"):
        name = event.split()[0].lower()
        _execute(
            f"CREATE TRIGGER qa_annotation_{name} BEFORE {event} ON annotation "
            "WHEN new.qa_question_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM qa_message WHERE id=new.qa_question_id AND role='question' "
            "AND paper_id=new.paper_id) "
            "BEGIN SELECT RAISE(ABORT, 'qa_annotation_question_invalid'); END"
        )


def upgrade() -> None:
    _create_table()
    _link_annotations()
    _execute(
        "CREATE TRIGGER qa_validate_identity BEFORE INSERT ON qa_message "
        "WHEN EXISTS (SELECT 1 FROM qa_message WHERE id=new.id AND ("
        "role IS NOT new.role OR paper_id IS NOT new.paper_id OR "
        "parent_id IS NOT new.parent_id OR request_id IS NOT new.request_id)) "
        "BEGIN SELECT RAISE(ABORT, 'qa_identity_conflict'); END"
    )
    _execute(
        "CREATE TRIGGER qa_validate_parent BEFORE INSERT ON qa_message "
        "WHEN new.role='answer' AND NOT EXISTS ("
        "SELECT 1 FROM qa_message WHERE id=new.parent_id AND role='question' "
        "AND paper_id=new.paper_id) "
        "BEGIN SELECT RAISE(ABORT, 'qa_parent_invalid'); END"
    )
    _execute(
        "CREATE TRIGGER qa_immutable BEFORE UPDATE ON qa_message "
        "BEGIN SELECT RAISE(ABORT, 'qa_message_immutable'); END"
    )
    _execute(
        "CREATE TRIGGER qa_search_insert AFTER INSERT ON qa_message BEGIN "
        "INSERT INTO memory_fts(kind,id,paper_id,page,authorship,source_version,title,text) "
        "VALUES ('qa',new.id,new.paper_id,new.page,"
        "CASE WHEN new.role='question' THEN 'user' ELSE 'model' END,'','',new.text); END"
    )
    _execute(
        "CREATE TRIGGER qa_search_delete AFTER DELETE ON qa_message BEGIN "
        "DELETE FROM memory_fts WHERE kind='qa' AND id=old.id; END"
    )


def downgrade() -> None:
    for name in (
        "qa_annotation_insert",
        "qa_annotation_update",
        "qa_search_delete",
        "qa_search_insert",
        "qa_immutable",
        "qa_validate_parent",
        "qa_validate_identity",
    ):
        _execute(f"DROP TRIGGER {name}")
    _execute("DELETE FROM memory_fts WHERE kind='qa'")
    _execute("ALTER TABLE annotation DROP COLUMN qa_question_id")
    op.drop_index("ix_qa_paper_created", table_name="qa_message")
    op.drop_table("qa_message")
