"""Alembic 运行环境。地址由程序传入，不读取 ini 文件。"""

from alembic import context
from sqlalchemy import create_engine, event

from litgleaner.infra.db.schema import metadata
from litgleaner.infra.db.schema_rules import include_schema_object
from litgleaner.infra.db.search_index import register_search_functions

engine = create_engine(context.config.attributes["database_url"])
event.listen(engine, "connect", register_search_functions)
with engine.connect() as connection:
    context.configure(
        connection=connection,
        target_metadata=metadata,
        render_as_batch=True,
        include_object=include_schema_object,
    )
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
