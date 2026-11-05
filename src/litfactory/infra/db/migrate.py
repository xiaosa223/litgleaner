"""数据库迁移的程序入口。"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine

from litfactory.infra.db.database import ensure_parent_dir

_MIGRATIONS = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS))
    config.attributes["database_url"] = url
    return config


def upgrade_database(url: str) -> None:
    ensure_parent_dir(url)
    command.upgrade(alembic_config(url), "head")


def is_up_to_date(url: str) -> bool:
    ensure_parent_dir(url)
    head = ScriptDirectory.from_config(alembic_config(url)).get_current_head()
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return MigrationContext.configure(connection).get_current_revision() == head
    finally:
        engine.dispose()
