"""FTS5 的虚表及其影子表由迁移管理，单独验证，不作为普通关系表比较。"""

from sqlalchemy.schema import SchemaItem

FTS_TABLES = frozenset(
    {
        "memory_fts",
        "memory_fts_data",
        "memory_fts_idx",
        "memory_fts_content",
        "memory_fts_docsize",
        "memory_fts_config",
    }
)


def include_schema_object(
    obj: SchemaItem, name: str | None, type_: str, reflected: bool, compare_to: SchemaItem | None
) -> bool:
    return not (type_ == "table" and name in FTS_TABLES)
