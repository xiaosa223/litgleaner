"""读写统一文档，并给 LLM 准备论文内容。"""

import re

from litgleaner.application.codec import compress, compress_json, decompress_json
from litgleaner.application.context import AppContext
from litgleaner.domain.chunker import ChunkedDocument, chunk_document
from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.evidence import select_chunks
from litgleaner.domain.paper import Paper

_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def document_key(sha256: str, parser_id: str) -> str:
    safe = _UNSAFE.sub("_", parser_id)
    return f"doc/{sha256[:2]}/{sha256[2:4]}/{sha256}.{safe}.json.zst"


def pdf_key(sha256: str) -> str:
    return f"pdf/{sha256[:2]}/{sha256[2:4]}/{sha256}.pdf"


def save_document(ctx: AppContext, document: NormalizedDocument) -> tuple[str, bytes]:
    key = document_key(document.source_pdf_sha256, document.parser_id)
    payload = compress_json(document.to_dict())
    ctx.blobs.put(key, payload)
    return key, payload


def save_parser_raw(
    ctx: AppContext, document: NormalizedDocument, raw: bytes | None
) -> tuple[str, int] | None:
    """解析器原始输出压缩保存，按保留策略短期留存，用于排查解析问题。"""
    if raw is None:
        return None
    sha256 = document.source_pdf_sha256
    safe = _UNSAFE.sub("_", document.parser_id)
    key = f"parser_raw/{sha256[:2]}/{sha256[2:4]}/{sha256}.{safe}.zst"
    payload = compress(raw)
    ctx.blobs.put(key, payload)
    return key, len(payload)


def load_document(ctx: AppContext, doc_key: str) -> NormalizedDocument:
    return NormalizedDocument.from_dict(decompress_json(ctx.blobs.get(doc_key)))


def load_chunks(ctx: AppContext, doc_key: str) -> ChunkedDocument:
    return chunk_document(load_document(ctx, doc_key), ctx.settings.chunker)


def full_text_of(chunked: ChunkedDocument) -> str:
    return "\n".join(chunk.text for chunk in chunked.chunks)


def paper_header(paper: Paper) -> str:
    return f"Title: {paper.title}\nAbstract: {paper.abstract or '（无摘要）'}"


def paper_text(ctx: AppContext, paper: Paper, chunked: ChunkedDocument | None) -> str:
    """元数据加按优先级筛选的正文，控制总 token。"""
    if chunked is None:
        return paper_header(paper)
    picked = select_chunks(chunked.chunks, ctx.settings.assess_token_budget)
    body = "\n\n".join(
        f"[p.{chunk.page_start + 1} {chunk.section_type}]\n{chunk.text}" for chunk in picked
    )
    return f"{paper_header(paper)}\n\n{body}"
