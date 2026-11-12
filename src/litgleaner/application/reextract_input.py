"""历史重提取的只读准备与完整请求冻结；归档不在数据库写锁内读取。"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_document
from litgleaner.application.extraction_schema import EXTRACT_SCHEMA
from litgleaner.application.knowledge_ingest import ExtractionSource, extraction_source
from litgleaner.application.stages.extract import extraction_input_hash, extraction_producer_version
from litgleaner.domain.chunker import chunk_document
from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.evidence import wrap_untrusted
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.domain.paper import Paper
from litgleaner.ports.reextract import ReextractSnapshot, metadata_revision


@dataclass(frozen=True)
class PreparedExtraction:
    manifest: Mapping[str, Any]
    document: NormalizedDocument
    source: ExtractionSource


class DocumentCache:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.documents: dict[str, NormalizedDocument | None] = {}

    def get(self, key: str) -> NormalizedDocument | None:
        if key not in self.documents:
            try:
                self.documents[key] = load_document(self.ctx, key)
            except Exception:
                self.documents[key] = None
        return self.documents[key]


def paper_revision(data: ReextractSnapshot, paper_id: str) -> str:
    return metadata_revision(
        tuple(paper for paper in data.papers if paper.id == paper_id),
        {key: value for key, value in data.fulltexts.items() if key == paper_id},
        tuple(row for row in data.extractions if row.paper_id == paper_id),
    )


def configuration(ctx: AppContext) -> Mapping[str, Any]:
    route = ctx.llm.routes.get("extract")
    language = ctx.settings.reading_language
    return {
        "producer_version": extraction_producer_version(ctx),
        "system": route.prompt.render({"output_language": language_name(language)})
        if route
        else None,
        "reading_language": language,
        "token_budget": ctx.settings.assess_token_budget,
        "chunker_version": ctx.settings.chunker.version,
    }


def prepare_paper(
    ctx: AppContext,
    data: ReextractSnapshot,
    paper: Paper,
    documents: DocumentCache,
) -> PreparedExtraction | None:
    state = data.fulltexts.get(paper.id)
    if state is None or state.doc_key is None:
        return None
    document = documents.get(state.doc_key)
    if document is None:
        return None
    source = extraction_source(
        paper,
        document,
        chunk_document(document, ctx.settings.chunker),
        ctx.settings.assess_token_budget,
    )
    latest = max((row.id for row in data.extractions if row.paper_id == paper.id), default=None)
    request_user = wrap_untrusted(source.text)
    manifest = {
        "version": "historical-reextract-v1",
        "paper_id": paper.id,
        "paper": asdict(paper),
        "latest_extraction_id": latest,
        "metadata_revision": paper_revision(data, paper.id),
        "doc_key": state.doc_key,
        "document_hash": stable_hash(document.to_dict()),
        "inputs": source.text,
        "source": asdict(source),
        "request_user": request_user,
        "input_hash": extraction_input_hash(
            state.doc_key,
            ctx.settings.chunker,
            ctx.settings.reading_language,
            ctx.settings.assess_token_budget,
        ),
        **configuration(ctx),
    }
    manifest["request_hash"] = stable_hash(
        {
            "system": manifest["system"],
            "user": request_user,
            "schema": EXTRACT_SCHEMA,
        }
    )
    return PreparedExtraction(manifest, document, source)


def job_key(prepared: PreparedExtraction) -> str:
    return f"reextract:{prepared.manifest['paper_id']}:{stable_hash(prepared.manifest)}"
