"""嵌入输入只依赖当前窄快照；旧提取退化为标题摘要，不自动重提取。"""

import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.stages.extract import extraction_input_hash, extraction_producer_version
from litgleaner.domain.embedding import INPUT_VERSION
from litgleaner.domain.embedding_views import EmbeddingInput, EmbeddingWarning
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper import Paper
from litgleaner.ports.store import Extraction, FullTextState


def _clean(value: Any) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()) if isinstance(value, str) else ""


def _text(data: Any, name: str) -> str:
    item = data.get(name) if isinstance(data, Mapping) else None
    return _clean(item.get("text")) if isinstance(item, Mapping) else ""


def _check(
    ctx: AppContext,
    state: FullTextState | None,
    extraction: Extraction,
    producer: str | None,
) -> tuple[bool, str | None]:
    if state is None or state.doc_key is None:
        return False, "missing_document"
    expected = extraction_input_hash(
        state.doc_key,
        ctx.settings.chunker,
        ctx.settings.reading_language,
        ctx.settings.assess_token_budget,
    )
    if extraction.input_hash != expected:
        return False, "stale_extraction_input"
    if producer is not None and extraction.extractor_version != producer:
        return False, "stale_extraction_producer"
    return True, "extraction_producer_unchecked" if producer is None else None


_DETAILS = {
    "missing_document": "缺少提取的当前文档指针，仅使用标题和摘要",
    "stale_extraction_input": "提取输入已过期，仅使用标题和摘要",
    "stale_extraction_producer": "提取生产者已过期，仅使用标题和摘要",
    "extraction_producer_unchecked": "提取输入可核对，未配置提取模型，生产者未完整核验",
    "empty_input": "没有可用的标题、摘要或提取文本，跳过本地向量",
}


def _bounded(parts: list[tuple[str, str]], limit: int) -> tuple[str, bool]:
    lines: list[str] = []
    used_extraction = False
    length = 0
    for name, value in parts:
        if not value:
            continue
        prefix = f"{name}: "
        value_start = length + bool(lines) + len(prefix)
        if name in {"problem", "approach"} and value_start < limit:
            used_extraction = True
        line = prefix + value
        length += bool(lines) + len(line)
        lines.append(line)
    return "\n".join(lines)[:limit], used_extraction


def _input(
    ctx: AppContext,
    paper: Paper,
    state: FullTextState | None,
    extraction: Extraction | None,
    producer: str | None,
) -> EmbeddingInput:
    parts = [("title", _clean(paper.title)), ("abstract", _clean(paper.abstract))]
    codes: list[str] = []
    if extraction is not None:
        usable, warning = _check(ctx, state, extraction, producer)
        if warning:
            codes.append(warning)
        if usable:
            extra = [(name, _text(extraction.data, name)) for name in ("problem", "approach")]
            parts.extend(extra)
    text, used_extraction = _bounded(parts, ctx.settings.embedding.max_input_chars)
    source_id = extraction.id if used_extraction and extraction is not None else None
    if not text:
        codes.append("empty_input")
    return EmbeddingInput(
        paper.id,
        text,
        stable_hash({"version": INPUT_VERSION, "text": text}),
        source_id,
        tuple(EmbeddingWarning(paper.id, code, _DETAILS[code]) for code in codes),
    )


def inputs(
    ctx: AppContext,
    papers: Sequence[Paper],
    fulltexts: Mapping[str, FullTextState],
    extractions: Sequence[Extraction],
) -> dict[str, EmbeddingInput]:
    latest = {item.paper_id: item for item in extractions}
    producer = extraction_producer_version(ctx)
    return {
        paper.id: _input(ctx, paper, fulltexts.get(paper.id), latest.get(paper.id), producer)
        for paper in papers
    }
