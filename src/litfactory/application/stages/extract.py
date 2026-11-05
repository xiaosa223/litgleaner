"""提取阶段：与主题无关的事实，每篇只做一次，切换主题时直接复用。"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from litfactory.application.context import AppContext
from litfactory.application.documents import full_text_of, load_chunks, paper_text
from litfactory.application.stages.base import Stage
from litfactory.domain.evidence import verify_quote
from litfactory.domain.hashing import stable_hash
from litfactory.ports.store import FullTextState, JobSpec

EXTRACT_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "conclusion", "limitations", "innovation"],
    "properties": {
        "summary": {"type": ["string", "null"], "maxLength": 300},
        "conclusion": {"type": ["string", "null"], "maxLength": 800},
        "innovation": {"type": ["string", "null"], "maxLength": 300},
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


def _with_verification(data: Mapping[str, Any], source_text: str) -> dict[str, Any]:
    limitations = [
        {**item, "verified": verify_quote(item.get("quote"), source_text)}
        for item in data["limitations"]
    ]
    return {**data, "limitations": limitations}


class ExtractStage(Stage):
    name = "extract"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        for paper_id in ctx.store.papers.parsed_without_extraction():
            state = ctx.store.papers.get_fulltext(paper_id)
            if state and state.doc_key:
                key = f"extract:{paper_id}:{stable_hash(state.doc_key)}"
                yield JobSpec(key, self.name, {"paper_id": paper_id})

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        paper_id = payload["paper_id"]
        paper = ctx.store.papers.get(paper_id)
        state = ctx.store.papers.get_fulltext(paper_id)
        if paper is None or state is None or state.doc_key is None:
            raise ValueError(f"没有可提取的文档：{paper_id}")
        chunked = load_chunks(ctx, state.doc_key)
        result = ctx.llm.call("extract", {}, paper_text(ctx, paper, chunked), EXTRACT_SCHEMA)
        data = _with_verification(result.data, full_text_of(chunked))
        now = ctx.now_iso()
        with ctx.store.atomic():
            ctx.store.papers.insert_extraction(
                paper_id, stable_hash(state.doc_key), result.prompt_version, data, now
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
