"""搜索副本的列权重与 SQLite 确定性函数注册。"""

from typing import Any

from litgleaner.domain.search_text import normalize_search_text

NORMALIZE_FUNCTION = "litgleaner_search_normalize_v1"
BM25_WEIGHTS = (0, 0, 0, 0, 0, 0, 0, 0, 4, 1)
RANK_SQL = "bm25(memory_fts,0,0,0,0,0,0,0,0,4,1)"


def _normalized(value: str | None) -> str:
    return normalize_search_text(value or "")


def register_search_functions(connection: Any, _record: Any = None) -> None:
    connection.create_function(NORMALIZE_FUNCTION, 1, _normalized, deterministic=True)
