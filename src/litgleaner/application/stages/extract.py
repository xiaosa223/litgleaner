"""提取阶段：与主题无关的事实，每篇只做一次，切换主题时直接复用。"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_chunks, load_document
from litgleaner.application.extraction_facts import verify_extraction
from litgleaner.application.extraction_schema import EXTRACT_SCHEMA, EXTRACTION_CONTRACT_VERSION
from litgleaner.application.knowledge_ingest import (
    ExtractionSource,
    extraction_source,
    ingest_concepts,
)
from litgleaner.application.stages.base import Stage
from litgleaner.domain.chunker import ChunkerConfig
from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.ports.store import FullTextState, JobSpec


def extraction_input_hash(
    doc_key: str,
    chunker: ChunkerConfig,
    reading_language: str = "zh-CN",
    token_budget: int = 6000,
) -> str:
    """文档、切分、语言与实际发送预算变化只标记失效，不自动重做。"""
    return stable_hash(
        {
            "doc": doc_key,
            "chunker": chunker.version,
            "reading_language": reading_language,
            "token_budget": token_budget,
            "contract": EXTRACTION_CONTRACT_VERSION,
            "schema": EXTRACT_SCHEMA,
        }
    )


def _input_hash(ctx: AppContext, doc_key: str) -> str:
    return extraction_input_hash(
        doc_key,
        ctx.settings.chunker,
        ctx.settings.reading_language,
        ctx.settings.assess_token_budget,
    )


def extraction_producer_version(ctx: AppContext) -> str | None:
    """提示词、输出结构与模型配置共同确定生产者；读取不调用模型。"""
    route = ctx.llm.routes.get("extract")
    if route is None:
        return None
    return stable_hash(
        {
            "contract": EXTRACTION_CONTRACT_VERSION,
            "schema": EXTRACT_SCHEMA,
            "prompt": route.prompt.version,
            "model": route.model,
            "provider": route.provider_version,
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
    expected = _input_hash(ctx, state.doc_key)
    producer = extraction_producer_version(ctx)
    return extraction.input_hash != expected or (
        producer is not None and extraction.extractor_version != producer
    )


def persist_extraction(
    ctx: AppContext,
    paper_id: str,
    input_hash: str,
    producer: str,
    *,
    data: Mapping[str, Any],
    document: NormalizedDocument,
    source: ExtractionSource,
) -> None:
    """调用方持有短事务；事实核验、概念及提取同进同退，不处理全文状态。"""
    verified = verify_extraction(data, document, source)
    now = ctx.now_iso()
    enriched = ingest_concepts(
        ctx,
        paper_id,
        verified,
        document,
        source,
        input_hash=input_hash,
        producer_version=producer,
        now=now,
    )
    ctx.store.papers.insert_extraction(paper_id, input_hash, producer, enriched, now)


class ExtractStage(Stage):
    name = "extract"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        for paper_id in ctx.store.papers.parsed_without_extraction():
            state = ctx.store.papers.get_fulltext(paper_id)
            if state and state.doc_key:
                input_hash = _input_hash(ctx, state.doc_key)
                producer = extraction_producer_version(ctx)
                key = f"extract:{paper_id}:{input_hash}:{producer}"
                yield JobSpec(
                    key,
                    self.name,
                    {"paper_id": paper_id, "input_hash": input_hash, "producer": producer},
                )

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        paper_id = payload["paper_id"]
        paper = ctx.store.papers.get(paper_id)
        state = ctx.store.papers.get_fulltext(paper_id)
        if paper is None or state is None or state.doc_key is None:
            raise ValueError(f"没有可提取的文档：{paper_id}")
        if ctx.store.papers.get_extraction(paper_id) is not None:
            return  # 与 plan 一致：历史结果只标记过期，重排/重复任务不自动重提取
        input_hash = _input_hash(ctx, state.doc_key)
        producer = extraction_producer_version(ctx)
        changed = payload.get("input_hash", input_hash) != input_hash or (
            payload.get("producer", producer) != producer
        )
        if changed and not (state.status == "failed" and payload.get("retry_current") is True):
            return  # 配置变化后，不让旧任务借用新配置再调用一次模型
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
        assert producer is not None  # call 已确认提取路由可用
        now = ctx.now_iso()
        with ctx.store.atomic():
            persist_extraction(
                ctx,
                paper_id,
                input_hash,
                producer,
                data=result.data,
                document=document,
                source=source,
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
