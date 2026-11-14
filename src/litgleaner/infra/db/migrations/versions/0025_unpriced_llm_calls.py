"""Allow unknown LLM cost (NULL) for models without a price (ADR 0013).

Revision ID: 0025
Revises: 0024

去掉 `llm_call.cost_usd` 的 NOT NULL 不改变磁盘上的数据格式，按 SQLite 官方文档
“Making Other Kinds Of Table Schema Changes” 的简化流程只改表定义文本：
不重建表、不动索引和根页，降级时逐字还原原定义。
"""

from alembic import op
from sqlalchemy import text

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

_STRICT = "cost_usd FLOAT NOT NULL"
_NULLABLE = "cost_usd FLOAT"


def _rewrite(old: str, new: str) -> None:
    bind = op.get_bind()
    sql = bind.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'llm_call'")
    ).scalar_one()
    if sql.count(old) != 1:
        raise RuntimeError(f"llm_call 表定义与预期不符，找不到唯一的 {old!r}")
    version = int(bind.execute(text("PRAGMA schema_version")).scalar_one())
    bind.execute(text("PRAGMA writable_schema = ON"))
    try:
        bind.execute(
            text("UPDATE sqlite_master SET sql = :sql WHERE type = 'table' AND name = 'llm_call'"),
            {"sql": sql.replace(old, new, 1)},
        )
        bind.execute(text(f"PRAGMA schema_version = {version + 1}"))
    finally:
        bind.execute(text("PRAGMA writable_schema = OFF"))
    problems = bind.execute(text("PRAGMA integrity_check")).scalars().all()
    if problems != ["ok"]:
        raise RuntimeError(f"修改 llm_call 定义后完整性检查失败：{problems[:3]}")


def upgrade() -> None:
    _rewrite(_STRICT, _NULLABLE)


def downgrade() -> None:
    # 价格未知的调用无法还原真实费用，回退时按 0 记；这是有损降级。
    op.execute("UPDATE llm_call SET cost_usd = 0 WHERE cost_usd IS NULL")
    _rewrite(_NULLABLE + ",", _STRICT + ",")
