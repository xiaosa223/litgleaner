"""Alembic 运行环境。地址由程序传入，不读取 ini 文件。"""

from alembic import context
from sqlalchemy import create_engine

from litfactory.infra.db.schema import metadata

engine = create_engine(context.config.attributes["database_url"])
with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
