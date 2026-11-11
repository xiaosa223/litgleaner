"""Persist topic recommendation reasons independently of assessment scores.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def _execute(sql: str) -> None:
    op.get_bind().exec_driver_sql(sql)


def _invalid_link() -> str:
    return (
        "CASE WHEN item.type='object' THEN "
        "coalesce(json_type(item.value,'$.goal_id'),'')!='text' OR "
        "length(json_extract(item.value,'$.goal_id'))=0 OR "
        "json_extract(item.value,'$.goal_id') GLOB '*[^A-Za-z0-9_-]*' OR "
        "coalesce(json_type(item.value,'$.reuse'),'')!='text' OR "
        "length(json_extract(item.value,'$.reuse')) NOT BETWEEN 1 AND 1000 OR "
        "coalesce(json_type(item.value,'$.quote'),'null') NOT IN ('text','null') OR "
        "length(json_extract(item.value,'$.quote'))>1600 OR "
        "coalesce(json_type(item.value,'$.page'),'null') NOT IN ('integer','null') OR "
        "coalesce(json_extract(item.value,'$.page'),0)<0 OR "
        "coalesce(json_type(item.value,'$.verified'),'false') NOT IN ('true','false') OR "
        "EXISTS (SELECT 1 FROM json_each(item.value) AS field "
        "WHERE field.key NOT IN ('goal_id','reuse','quote','page','verified')) "
        "ELSE 1 END"
    )


def upgrade() -> None:
    _execute(
        "ALTER TABLE assessment ADD COLUMN goal_links TEXT NOT NULL DEFAULT '[]' "
        "CONSTRAINT ck_assessment_goal_links CHECK ("
        "CASE WHEN json_valid(goal_links) THEN json_type(goal_links)='array' ELSE 0 END)"
    )
    for event in ("INSERT", "UPDATE OF goal_links"):
        suffix = "insert" if event == "INSERT" else "update"
        _execute(
            f"CREATE TRIGGER assessment_goal_links_{suffix} BEFORE {event} ON assessment "
            "WHEN CASE WHEN json_valid(new.goal_links) THEN EXISTS "
            "(SELECT 1 FROM json_each(new.goal_links) AS item WHERE " + _invalid_link() + ") "
            "ELSE 0 END BEGIN SELECT RAISE(ABORT, 'assessment_goal_links_invalid'); END"
        )


def downgrade() -> None:
    for event in ("insert", "update"):
        _execute(f"DROP TRIGGER assessment_goal_links_{event}")
    _execute("ALTER TABLE assessment DROP COLUMN goal_links")
