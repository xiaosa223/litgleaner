"""概念、出现位置和已核验定义的只读接口。"""

from fastapi import FastAPI, Query

from litgleaner.application import knowledge
from litgleaner.server.runtime import Context


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
