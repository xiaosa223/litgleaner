"""Normalize legacy Chinese translation cache language without dropping cache rows.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def _rename(source: str, target: str) -> None:
    op.get_bind().exec_driver_sql(
        "UPDATE translation SET target_lang = ? WHERE target_lang = ? AND NOT EXISTS ("
        "SELECT 1 FROM translation other WHERE other.unit=translation.unit "
        "AND other.source_hash=translation.source_hash "
        "AND other.glossary_hash=translation.glossary_hash "
        "AND other.translator_version=translation.translator_version AND other.target_lang=?)",
        (target, source, target),
    )


def upgrade() -> None:
    _rename("zh", "zh-CN")


def downgrade() -> None:
    _rename("zh-CN", "zh")
