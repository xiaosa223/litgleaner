"""数据库迁移的程序入口。"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from litgleaner.infra.db.database import ensure_parent_dir

_MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.attributes["database_url"] = url
    return config


def upgrade_database(url: str) -> None:
    ensure_parent_dir(url)
    command.upgrade(alembic_config(url), "head")


def _scripts() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    return ScriptDirectory.from_config(config)


def head_revision() -> str:
    head = _scripts().get_current_head()
    if head is None:
        raise RuntimeError("迁移目录里没有任何版本")
    return head


def known_revisions() -> frozenset[str]:
    return frozenset(script.revision for script in _scripts().walk_revisions())


def current_revision(url: str) -> str | None:
    """数据库当前的迁移版本；还没建表时为 None。"""
    ensure_parent_dir(url)
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()


def is_up_to_date(url: str) -> bool:
    return current_revision(url) == head_revision()
