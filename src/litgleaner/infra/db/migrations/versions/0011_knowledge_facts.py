"""Global concepts, normalized aliases and immutable verified paper occurrences.

Revision ID: 0011
Revises: 0010
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

_TABLES = ("concept", "concept_alias", "concept_occurrence")
_COLUMNS = "kind,id,paper_id,page,authorship,source_version,title,text"


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def _create_names() -> None:
    op.create_table(
        "concept",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_concept_name"),
    )
    op.create_table(
        "concept_alias",
        sa.Column("normalized_name", sa.String, primary_key=True),
        sa.Column("concept_id", sa.String, sa.ForeignKey("concept.id"), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("language", sa.String),
        sa.Column("created_at", sa.String, nullable=False),
        sa.CheckConstraint("length(trim(normalized_name)) > 0", name="ck_concept_alias_key"),
        sa.CheckConstraint("length(trim(name)) > 0", name="ck_concept_alias_name"),
    )
    op.create_index("ix_concept_alias_concept", "concept_alias", ["concept_id"])


def _create_occurrence() -> None:
    op.create_table(
        "concept_occurrence",
        sa.Column("concept_id", sa.String, sa.ForeignKey("concept.id"), primary_key=True),
        sa.Column("paper_id", sa.String, sa.ForeignKey("paper.id"), primary_key=True),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("definition", sa.Text),
        sa.Column("page", sa.Integer, nullable=False),
        sa.Column("bbox", sa.Text),
        sa.Column("verified", sa.Boolean, nullable=False),
        sa.Column("input_hash", sa.String, nullable=False),
        sa.Column("producer_version", sa.String, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
        sa.CheckConstraint("role IN ('core','prerequisite','mentioned')", name="ck_concept_role"),
        sa.CheckConstraint("typeof(page) = 'integer' AND page >= 0", name="ck_concept_page"),
        sa.CheckConstraint("verified IN (0,1)", name="ck_concept_verified"),
        sa.CheckConstraint(
            "(verified=1 AND definition IS NOT NULL AND length(trim(definition))>=10) "
            "OR (verified=0 AND definition IS NULL)",
            name="ck_concept_definition",
        ),
        sa.CheckConstraint("bbox IS NULL OR json_valid(bbox)", name="ck_concept_bbox"),
    )
    op.create_index("ix_concept_occurrence_paper", "concept_occurrence", ["paper_id"])


def _project(key: str) -> str:
    # 名称/别名的私有行让尚无论文位置的概念也可检索；全局搜索只展示 concept 行。
    aliases = (
        "coalesce((SELECT group_concat(a.name || ' ' || a.normalized_name,' ') "
        "FROM concept_alias a WHERE a.concept_id=c.id),'')"
    )
    definitions = (
        "coalesce((SELECT group_concat(d.definition,' ') FROM concept_occurrence d "
        "WHERE d.concept_id=c.id AND d.verified=1),'')"
    )
    return (
        "DELETE FROM memory_fts WHERE kind IN ('concept','concept_name') AND id=" + key + ";"
        f"INSERT INTO memory_fts({_COLUMNS}) "
        f"SELECT 'concept_name',c.id,'',NULL,'author','',c.name,{aliases} || ' ' || {definitions} "
        f"FROM concept c WHERE c.id={key};"
        f"INSERT INTO memory_fts({_COLUMNS}) "
        "SELECT 'concept',c.id,o.paper_id,o.page,'author','',c.name,c.name || ' ' || "
        "CASE WHEN o.verified=1 THEN coalesce(o.definition,'') ELSE '' END "
        "FROM concept_occurrence o JOIN concept c ON c.id=o.concept_id "
        f"WHERE c.id={key};"
    )


def upgrade() -> None:
    _create_names()
    _create_occurrence()
    for table in _TABLES:
        _execute(
            f"CREATE TRIGGER knowledge_immutable_{table} BEFORE UPDATE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'knowledge_fact_immutable'); END"
        )
        for event in ("INSERT", "DELETE"):
            alias = "new" if event == "INSERT" else "old"
            key = f"{alias}.id" if table == "concept" else f"{alias}.concept_id"
            _execute(
                f"CREATE TRIGGER knowledge_search_{table}_{event.lower()} "
                f"AFTER {event} ON {table} BEGIN {_project(key)} END"
            )


def downgrade() -> None:
    for table in _TABLES:
        _execute(f"DROP TRIGGER knowledge_immutable_{table}")
        for event in ("insert", "delete"):
            _execute(f"DROP TRIGGER knowledge_search_{table}_{event}")
    _execute("DELETE FROM memory_fts WHERE kind IN ('concept','concept_name')")
    op.drop_index("ix_concept_occurrence_paper", table_name="concept_occurrence")
    op.drop_index("ix_concept_alias_concept", table_name="concept_alias")
    for table in reversed(_TABLES):
        op.drop_table(table)
