"""概念、出现位置和已核验定义的只读接口。"""

from fastapi import FastAPI, Query
from pydantic import Field

from litgleaner.application import corrections, knowledge, notes
from litgleaner.server.runtime import Context
from litgleaner.server.schemas import _Body


class MetadataBody(_Body):
    """只提交要改的字段；空串表示撤销修正、恢复来源元数据。"""

    title: str | None = Field(default=None, max_length=500)
    authors: str | None = Field(default=None, max_length=2000)
    venue: str | None = Field(default=None, max_length=300)
    published_at: str | None = Field(default=None, max_length=20)


def install_knowledge_routes(app: FastAPI) -> None:
    @app.get("/api/knowledge/concepts")
    def concepts(
        ctx: Context,
        q: str = Query(default="", max_length=500),
        topic_id: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> knowledge.KnowledgePage:
        return knowledge.concepts(ctx, topic_id=topic_id, query=q, limit=limit, offset=offset)

    @app.get("/api/knowledge/concepts/{concept_id}")
    def concept(ctx: Context, concept_id: str) -> knowledge.ConceptDetail:
        return knowledge.concept(ctx, concept_id)

    @app.get("/api/papers/{paper_id}/concepts")
    def paper_concepts(ctx: Context, paper_id: str) -> tuple[knowledge.PaperConcept, ...]:
        return knowledge.paper_concepts(ctx, paper_id)

    @app.get("/api/notes")
    def all_notes(
        ctx: Context,
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> tuple[notes.NoteView, ...]:
        return notes.recent(ctx, limit, offset)

    @app.get("/api/papers/{paper_id}/metadata")
    def metadata(ctx: Context, paper_id: str) -> corrections.MetadataView:
        return corrections.view(ctx, paper_id)

    @app.put("/api/papers/{paper_id}/metadata")
    def correct_metadata(
        ctx: Context, paper_id: str, body: MetadataBody
    ) -> corrections.MetadataView:
        return corrections.save(ctx, paper_id, body.model_dump(exclude_none=True))
