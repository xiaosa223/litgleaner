"""关系输入仅使用当前提取和可定位到真实原页的连续作者片段。"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_document
from litgleaner.application.extraction_facts import verify_extraction
from litgleaner.application.knowledge_ingest import ExtractionSource, SentBlock
from litgleaner.application.stages.extract import (
    extraction_input_hash,
    extraction_producer_version,
)
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.evidence import MIN_QUOTE_CHARS, clean_untrusted, verify_quote
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper import Paper
from litgleaner.ports.memory import MemorySnapshot
from litgleaner.ports.store import Extraction, FullTextState


@dataclass(frozen=True)
class RelateSource:
    paper_id: str
    doc_key: str
    document: NormalizedDocument
    source: ExtractionSource
    record: Mapping[str, Any]
    source_hash: str


@dataclass
class RelatePreparationCache:
    """只在一次预览或重核对中共用；包含不可读结果，之后须新建。"""

    documents: dict[str, NormalizedDocument | None] = field(default_factory=dict)

    def document(self, ctx: AppContext, doc_key: str) -> NormalizedDocument | None:
        if doc_key not in self.documents:
            self.documents[doc_key] = _read_document(ctx, doc_key)
        return self.documents[doc_key]


def source_hash(paper: Paper, fulltext: FullTextState | None, extraction: Extraction | None) -> str:
    """来源元数据的纯指纹；真实 blob 可读性另行验证，不在此读库或归档。"""
    return stable_hash(
        {
            "paper_id": paper.id,
            "title": paper.title,
            "document": None
            if fulltext is None
            else {"doc_key": fulltext.doc_key, "status": fulltext.status},
            "extraction": None
            if extraction is None
            else {
                "input": extraction.input_hash,
                "producer": extraction.extractor_version,
                "data": extraction.data,
            },
        }
    )


def _current(ctx: AppContext, state: FullTextState | None, extraction: Extraction | None) -> bool:
    if state is None or state.doc_key is None or extraction is None:
        return False
    expected = extraction_input_hash(
        state.doc_key,
        ctx.settings.chunker,
        ctx.settings.reading_language,
        ctx.settings.assess_token_budget,
    )
    producer = extraction_producer_version(ctx)
    return extraction.input_hash == expected and (
        producer is None or extraction.extractor_version == producer
    )


def _body(block: Block) -> str:
    return "\n".join(value for value in (block.caption, block.text) if value)


def _located(document: NormalizedDocument, quote: Any, page: Any = None) -> SentBlock | None:
    if not isinstance(quote, str) or len(" ".join(quote.split())) < MIN_QUOTE_CHARS:
        return None
    pattern = r"\s+".join(re.escape(part) for part in quote.split())
    for block in document.blocks:
        if (
            type(block.page) is not int
            or block.page < 0
            or (page is not None and block.page != page)
        ):
            continue
        match = re.search(pattern, _body(block), re.IGNORECASE)
        if match is not None and verify_quote(quote, match.group()):
            return SentBlock(block, match.group())
    return None


def _field_quotes(data: Mapping[str, Any]) -> Iterable[tuple[str, Mapping[str, Any]]]:
    for name in ("problem", "approach", "method", "claims"):
        raw = data.get(name)
        items = raw if isinstance(raw, list) else (raw,)
        for item in items:
            if isinstance(item, Mapping) and item.get("verified") is True:
                yield name, item


def _parts(
    ctx: AppContext, extraction: Extraction, document: NormalizedDocument, contexts: Iterable[str]
) -> tuple[tuple[SentBlock, str], ...]:
    data = extraction.data if isinstance(extraction.data, Mapping) else {}
    candidates = [
        (name, _located(document, item.get("quote"), item.get("page")))
        for name, item in _field_quotes(data)
        if type(item.get("page")) is int and item["page"] >= 0
    ]
    candidates.extend(("citation", _located(document, text)) for text in contexts)
    found: list[tuple[SentBlock, str]] = []
    for name, located in candidates:
        if located is None:
            continue
        text = clean_untrusted(located.text[: ctx.settings.relate.fragment_chars])
        sent = SentBlock(located.block, text)
        source = ExtractionSource(text, (sent,), ((0, len(text)),))
        valid = verify_extraction(
            {"claims": [{"quote": text, "page": sent.page}]}, document, source
        )
        if not valid["claims"][0]["verified"] or any(s == sent for s, _ in found):
            continue
        found.append((sent, name))
        if len(found) == ctx.settings.relate.max_fragments_per_side:
            break
    return tuple(found)


def _source(
    ctx: AppContext,
    paper: Paper,
    state: FullTextState,
    extraction: Extraction,
    document: NormalizedDocument,
    *,
    contexts: Iterable[str],
) -> RelateSource | None:
    parts = _parts(ctx, extraction, document, contexts)
    if not parts or state.doc_key is None:
        return None
    text, spans, fragments = "", [], []
    for sent, category in parts:
        marker = f"\n[page={sent.page} field={category}]\n"
        start = len(text) + len(marker)
        text += marker + sent.text
        spans.append((start, len(text)))
        fragments.append({"field": category, "page": sent.page, "quote": sent.text})
    record = {
        "paper_id": paper.id,
        "doc_key": state.doc_key,
        "title": clean_untrusted(paper.title[:1000]),
        "fragments": fragments,
    }
    return RelateSource(
        paper.id,
        state.doc_key,
        document,
        ExtractionSource(text, tuple(sent for sent, _ in parts), tuple(spans)),
        record,
        source_hash(paper, state, extraction),
    )


def prepare_sources(
    ctx: AppContext,
    data: MemorySnapshot,
    ids: frozenset[str],
    *,
    cache: RelatePreparationCache | None = None,
) -> tuple[dict[str, RelateSource], tuple[str, ...]]:
    extractions = {value.paper_id: value for value in sorted(data.extractions, key=lambda e: e.id)}
    cache = cache if cache is not None else RelatePreparationCache()
    sources, warnings = {}, []
    contexts: dict[str, list[str]] = {}
    for citation in data.citations:
        contexts.setdefault(citation.citing_paper_id, []).extend(citation.contexts)
    for paper in data.papers:
        if paper.id not in ids:
            continue
        state, extraction = data.fulltexts.get(paper.id), extractions.get(paper.id)
        if not _current(ctx, state, extraction):
            warnings.append(f"{paper.id}：缺少当前提取或文档指针，未发送")
            continue
        assert state is not None and state.doc_key is not None and extraction is not None
        document = cache.document(ctx, state.doc_key)
        source = (
            None
            if document is None
            else _source(
                ctx, paper, state, extraction, document, contexts=contexts.get(paper.id, ())
            )
        )
        if source is None:
            warnings.append(f"{paper.id}：文档不可读或没有可定位的连续证据，未发送")
        else:
            sources[paper.id] = source
    return sources, tuple(warnings)


def refresh_sources(
    ctx: AppContext, sources: Mapping[str, RelateSource]
) -> dict[str, RelateSource]:
    """调用结束后重新读取当前归档；保留冻结发送范围，坏来源逐侧移除。"""
    cache = RelatePreparationCache()
    refreshed = {}
    for paper_id, source in sources.items():
        document = cache.document(ctx, source.doc_key)
        if document is not None:
            located = tuple(
                _located(document, sent.text, sent.page) for sent in source.source.sources
            )
            if all(item is not None for item in located):
                blocks = tuple(
                    replace(sent, block=item.block)
                    for sent, item in zip(source.source.sources, located, strict=True)
                    if item is not None
                )
                refreshed[paper_id] = replace(
                    source, document=document, source=replace(source.source, sources=blocks)
                )
    return refreshed


def _read_document(ctx: AppContext, doc_key: str) -> NormalizedDocument | None:
    try:
        document = load_document(ctx, doc_key)
        invalid = any(
            type(block.page) is not int
            or block.page < 0
            or (block.caption is not None and not isinstance(block.caption, str))
            for block in document.blocks
        )
        return None if invalid else document
    except Exception:
        return None
