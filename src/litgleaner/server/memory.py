"""论文间事实查询的本地只读接口。"""

from fastapi import FastAPI, Query

from litgleaner.application import (
    embeddings,
    memory,
    memory_candidates,
    reader_open,
    reading_memory,
)
from litgleaner.domain.embedding_views import SimilarPapers
from litgleaner.domain.important_works import ImportantWorks
from litgleaner.domain.memory import MemoryFacts
from litgleaner.domain.memory_candidate_views import MemoryCandidates
from litgleaner.domain.reading import ReadingState
from litgleaner.domain.reading_memory import ReadingMemory
from litgleaner.server.runtime import Context


def install_memory_routes(app: FastAPI) -> None:
    @app.get("/api/memory/important-works")
    def important_works(
        ctx: Context,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> ImportantWorks:
        return memory.important_works(ctx, limit=limit, offset=offset)

    @app.post("/api/papers/{paper_id}/open")
    def open_paper(ctx: Context, paper_id: str) -> ReadingState:
        return reader_open.open_paper(ctx, paper_id)

    @app.get("/api/papers/{paper_id}/memory-candidates")
    def candidates(
        ctx: Context,
        paper_id: str,
        limit: int | None = Query(default=None, ge=1, le=200),
    ) -> MemoryCandidates:
        return memory_candidates.candidates(ctx, paper_id, limit=limit)

    @app.get("/api/papers/{paper_id}/memory-similar")
    def similar(
        ctx: Context,
        paper_id: str,
        limit: int = Query(default=5, ge=1, le=200),
    ) -> SimilarPapers:
        return embeddings.similar(ctx, paper_id, limit=limit)

    @app.get("/api/papers/{paper_id}/memory-facts")
    def facts(
        ctx: Context,
        paper_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> MemoryFacts:
        return memory.facts(ctx, paper_id, limit=limit, offset=offset)

    @app.get("/api/papers/{paper_id}/reading-memory")
    def reading_summary(
        ctx: Context,
        paper_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> ReadingMemory:
        return reading_memory.view(ctx, paper_id, limit=limit, offset=offset)
