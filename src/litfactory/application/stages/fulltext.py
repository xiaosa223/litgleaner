"""取全文和解析。只处理初筛通过的论文，这是控制成本和存储的关键。"""

import hashlib
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from litfactory.application.context import AppContext
from litfactory.application.documents import load_chunks, pdf_key, save_document
from litfactory.application.stages.base import Stage
from litfactory.application.topics import active_topics
from litfactory.ports.fulltext import FullText, FullTextRequest
from litfactory.ports.store import Artifact, FullTextState, JobSpec

logger = logging.getLogger(__name__)
_PDF_MAGIC = b"%PDF-"


def _first_available(ctx: AppContext, request: FullTextRequest) -> FullText | None:
    for provider in ctx.fulltext:
        found = provider.fetch(request)
        if found is None:
            continue
        if found.content.startswith(_PDF_MAGIC):
            return found
        logger.warning("%s 返回的内容不是 PDF，已忽略：%s", provider.name, request.paper_id)
    return None


class AcquireStage(Stage):
    name = "acquire"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        papers = {
            paper_id
            for topic in active_topics(ctx).values()
            for paper_id in ctx.store.judgments.relevant_without_fulltext(
                topic.id, topic.topic_hash
            )
        }
        for paper_id in sorted(papers):
            yield JobSpec(f"acquire:{paper_id}", self.name, {"paper_id": paper_id})

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        paper_id = payload["paper_id"]
        paper = ctx.store.papers.get(paper_id)
        if paper is None:
            raise ValueError(f"论文不存在：{paper_id}")
        request = FullTextRequest(paper_id, paper.title, ctx.store.papers.aliases_of(paper_id))
        found = _first_available(ctx, request)
        now = ctx.now_iso()
        if found is None:
            ctx.store.papers.save_fulltext(FullTextState(paper_id, "unavailable"), now)
            return
        sha256 = hashlib.sha256(found.content).hexdigest()
        key = pdf_key(sha256)
        ctx.blobs.put(key, found.content)
        with ctx.store.atomic():
            ctx.store.papers.add_artifact(
                Artifact(
                    sha256, "pdf", key, len(found.content), found.provider, found.license_basis
                ),
                now,
            )
            ctx.store.papers.save_fulltext(
                FullTextState(
                    paper_id,
                    "available",
                    pdf_sha256=sha256,
                    provider=found.provider,
                    license_basis=found.license_basis,
                ),
                now,
            )

    def on_dead(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        """取全文彻底失败时仍然按摘要评估，论文不能因此从系统里消失。"""
        state = FullTextState(payload["paper_id"], "failed")
        ctx.store.papers.save_fulltext(state, ctx.now_iso())


class ParseStage(Stage):
    name = "parse"

    def plan(self, ctx: AppContext) -> Iterable[JobSpec]:
        for paper_id in ctx.store.papers.papers_with_fulltext_status("available"):
            state = ctx.store.papers.get_fulltext(paper_id)
            if state and state.pdf_sha256:
                key = f"parse:{paper_id}:{state.pdf_sha256}:{ctx.parser.parser_id}"
                yield JobSpec(key, self.name, {"paper_id": paper_id})

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        state = ctx.store.papers.get_fulltext(payload["paper_id"])
        if state is None or state.pdf_sha256 is None:
            raise ValueError(f"没有可解析的 PDF：{payload['paper_id']}")
        pdf = ctx.blobs.get(pdf_key(state.pdf_sha256))
        document = ctx.parser.parse(pdf, state.pdf_sha256)
        key, stored = save_document(ctx, document)
        chunked = load_chunks(ctx, key)
        now = ctx.now_iso()
        with ctx.store.atomic():
            ctx.store.papers.add_artifact(
                Artifact(state.pdf_sha256, "doc", key, len(stored), document.parser_id, None), now
            )
            ctx.store.papers.save_fulltext(
                FullTextState(
                    state.paper_id,
                    "parsed",
                    pdf_sha256=state.pdf_sha256,
                    doc_key=key,
                    provider=state.provider,
                    license_basis=state.license_basis,
                    quality=chunked.quality,
                ),
                now,
            )

    def on_dead(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        state = ctx.store.papers.get_fulltext(payload["paper_id"])
        failed = FullTextState(
            payload["paper_id"],
            "failed",
            pdf_sha256=state.pdf_sha256 if state else None,
            provider=state.provider if state else None,
            license_basis=state.license_basis if state else None,
        )
        ctx.store.papers.save_fulltext(failed, ctx.now_iso())
