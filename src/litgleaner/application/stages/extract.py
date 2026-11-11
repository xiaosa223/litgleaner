"""提取阶段：与主题无关的事实，每篇只做一次，切换主题时直接复用。"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.documents import full_text_of, load_chunks, load_document
from litgleaner.application.knowledge_ingest import extraction_source, ingest_concepts
from litgleaner.application.stages.base import Stage
from litgleaner.domain.chunker import ChunkerConfig
from litgleaner.domain.evidence import clean_untrusted, verify_quote
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.ports.store import FullTextState, JobSpec

EXTRACT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "conclusion", "limitations", "innovation", "key_concepts"],
    "properties": {
        "summary": {"type": ["string", "null"], "maxLength": 300},
        "conclusion": {"type": ["string", "null"], "maxLength": 800},
        "innovation": {"type": ["string", "null"], "maxLength": 300},
        "key_concepts": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "role", "definition", "page"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "role": {"enum": ["core", "prerequisite", "mentioned"]},
                    "definition": {"type": ["string", "null"], "maxLength": 2000},
                    "page": {"type": "integer", "minimum": 0},
                },
            },
        },
        "limitations": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "source", "quote"],
                "properties": {
                    "text": {"type": "string", "maxLength": 400},
                    "source": {"enum": ["author", "inferred"]},
                    "quote": {"type": ["string", "null"], "maxLength": 800},
                },
            },
        },
    },
}


def _with_verification(
    data: Mapping[str, Any], source_text: str, sent_text: str | None = None
) -> dict[str, Any]:
    limitations = [
        {
            **item,
            "verified": verify_quote(item.get("quote"), source_text)
            and verify_quote(
                item.get("quote"), clean_untrusted(source_text if sent_text is None else sent_text)
            ),
        }
        for item in data["limitations"]
    ]
    return {**data, "limitations": limitations}


def extraction_input_hash(
    doc_key: str, chunker: ChunkerConfig, reading_language: str = "zh-CN"
) -> str:
    """提取输入包含文档、切分版本和阅读语言；变化只标记失效，不自动重做。"""
    return stable_hash(
        {
            "doc": doc_key,
            "chunker": chunker.version,
            "reading_language": reading_language,
        }
    )


def extraction_is_stale(ctx: AppContext, paper_id: str) -> bool:
    """已有提取是否落后于当前输入或提示词；只读，不调用模型。"""
    extraction = ctx.store.papers.get_extraction(paper_id)
    if extraction is None:
        return False
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is None or state.doc_key is None:
        return True
    expected = extraction_input_hash(
        state.doc_key, ctx.settings.chunker, ctx.settings.reading_language
    )
    route = ctx.llm.routes.get("extract")
    return extraction.input_hash != expected or (
        route is not None and extraction.extractor_version != route.prompt.version
    )


class ExtractStage(Stage):
    name = "extract"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        for paper_id in ctx.store.papers.parsed_without_extraction():
            state = ctx.store.papers.get_fulltext(paper_id)
            if state and state.doc_key:
                input_hash = extraction_input_hash(
                    state.doc_key, ctx.settings.chunker, ctx.settings.reading_language
                )
                key = f"extract:{paper_id}:{input_hash}"
                yield JobSpec(key, self.name, {"paper_id": paper_id, "input_hash": input_hash})

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        paper_id = payload["paper_id"]
        paper = ctx.store.papers.get(paper_id)
        state = ctx.store.papers.get_fulltext(paper_id)
        if paper is None or state is None or state.doc_key is None:
            raise ValueError(f"没有可提取的文档：{paper_id}")
        input_hash = extraction_input_hash(
            state.doc_key, ctx.settings.chunker, ctx.settings.reading_language
        )
        if payload.get("input_hash", input_hash) != input_hash:
            return  # 配置变化后，不让旧任务借用新配置再调用一次模型
        if ctx.store.papers.get_extraction(paper_id) is not None:
            return  # 与 plan 一致：历史结果只标记过期，重排/重复任务不自动重提取
        chunked = load_chunks(ctx, state.doc_key)
        document = load_document(ctx, state.doc_key)
        source = extraction_source(paper, document, chunked, ctx.settings.assess_token_budget)
        result = ctx.llm.call(
            "extract",
            {"output_language": language_name(ctx.settings.reading_language)},
            source.text,
            EXTRACT_SCHEMA,
            paper_id=paper.id,
        )
        data = _with_verification(result.data, full_text_of(chunked), source.text)
        now = ctx.now_iso()
        with ctx.store.atomic():
            data = ingest_concepts(
                ctx,
                paper_id,
                data,
                document,
                source,
                input_hash=input_hash,
                producer_version=result.prompt_version,
                now=now,
            )
            ctx.store.papers.insert_extraction(
                paper_id,
                input_hash,
                result.prompt_version,
                data,
                now,
            )
            # 死信任务重试成功时，论文要从“只读摘要”恢复，之后才能按全文重新评估
            ctx.store.papers.save_fulltext(replace(state, status="parsed"), now)

    def on_dead(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        state = ctx.store.papers.get_fulltext(payload["paper_id"])
        failed = FullTextState(
            payload["paper_id"],
            "failed",
            pdf_sha256=state.pdf_sha256 if state else None,
            doc_key=state.doc_key if state else None,
            provider=state.provider if state else None,
            license_basis=state.license_basis if state else None,
            quality=state.quality if state else None,
        )
        ctx.store.papers.save_fulltext(failed, ctx.now_iso())
