"""论文间事实查询的本地只读接口。"""

from fastapi import FastAPI, Query

from litgleaner.application import memory
from litgleaner.domain.memory import MemoryFacts
from litgleaner.server.runtime import Context


def install_memory_routes(app: FastAPI) -> None:
    @app.get("/api/papers/{paper_id}/memory-facts")
    def facts(
        ctx: Context,
        paper_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> MemoryFacts:
        return memory.facts(ctx, paper_id, limit=limit, offset=offset)
