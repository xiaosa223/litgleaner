"""score undetermined dimensions, digest item provisional flag

Revision ID: 0004
Revises: 0003
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 无法判断的维度（JSON 数组）。旧分数没有记录，按空数组处理
    with op.batch_alter_table("score", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("undetermined", sa.Text(), nullable=False, server_default="[]")
        )
    # 推送时是否为临时分，用于“读完全文后升级”补推一次。旧数据从卡片内容回填
    with op.batch_alter_table("digest_item", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("provisional", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.execute(
        "UPDATE digest_item SET provisional = COALESCE(json_extract(card, '$.provisional'), 0)"
    )


def downgrade() -> None:
    with op.batch_alter_table("digest_item", schema=None) as batch_op:
        batch_op.drop_column("provisional")
    with op.batch_alter_table("score", schema=None) as batch_op:
        batch_op.drop_column("undetermined")
