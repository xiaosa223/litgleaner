"""本地引文查询及用户显式发起的参考文献刷新。"""

from fastapi import FastAPI
from pydantic import Field

from litgleaner.application import citations, references
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import _Body


class ReferenceBody(_Body):
    sources: list[str] | None = Field(default=None, min_length=1, max_length=20)


def install_citation_routes(app: FastAPI) -> None:
    @app.get("/api/papers/{paper_id}/citations")
    def for_paper(ctx: Context, paper_id: str) -> citations.CitationList:
        return citations.for_paper(ctx, paper_id)

    @app.post("/api/papers/{paper_id}/citations/index")
    def index_parsed(ctx: Context, paper_id: str) -> citations.CitationList:
        return citations.index_parsed(ctx, paper_id)

    @app.post("/api/papers/{paper_id}/references/refresh")
    def refresh(ctx: Context, paper_id: str, body: ReferenceBody) -> references.ReferenceRefresh:
        names = tuple(body.sources) if body.sources is not None else None
        return references.refresh(ctx, paper_id, names)
