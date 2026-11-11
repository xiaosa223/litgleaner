"""取全文和解析。只处理初筛通过的论文和用户添加的论文，这是控制成本和存储的关键。"""

import hashlib
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_chunks, pdf_key, save_document, save_parser_raw
from litgleaner.application.stages.base import Stage
from litgleaner.application.topics import active_topics
from litgleaner.domain.errors import LitGleanerError, PermanentError
from litgleaner.ports.fulltext import FullText, FullTextProvider, FullTextRequest
from litgleaner.ports.store import Artifact, FullTextState, JobSpec

logger = logging.getLogger(__name__)
_PDF_MAGIC = b"%PDF-"


def first_available(
    providers: Sequence[FullTextProvider], request: FullTextRequest
) -> FullText | None:
    """按配置顺序尝试。单个来源出错不影响后面的来源；都没拿到且有过错误时，
    抛出第一个错误交给调度重试，而不是把论文错标成“没有全文”。"""
    errors: list[LitGleanerError] = []
    for provider in providers:
        try:
            found = provider.fetch(request)
        except LitGleanerError as error:
            logger.warning("全文来源 %s 出错：%s", provider.name, error)
            errors.append(error)
            continue
        if found is None:
            continue
        if found.content.startswith(_PDF_MAGIC):
            return found
        logger.warning("%s 返回的内容不是 PDF，已忽略：%s", provider.name, request.paper_id)
    retryable = [error for error in errors if not isinstance(error, PermanentError)]
    if retryable or errors:
        raise (retryable or errors)[0]
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
        papers.update(ctx.store.inbox.papers_without_fulltext())  # 用户添加的论文不要求通过初筛
        for paper_id in sorted(papers):
            yield JobSpec(f"acquire:{paper_id}", self.name, {"paper_id": paper_id})

    def process(self, ctx: AppContext, payload: Mapping[str, Any]) -> None:
        paper_id = payload["paper_id"]
        current = ctx.store.papers.get_fulltext(paper_id)
        if current and current.pdf_sha256:
            return
        paper = ctx.store.papers.get(paper_id)
        if paper is None:
            raise ValueError(f"论文不存在：{paper_id}")
        request = FullTextRequest(paper_id, paper.title, ctx.store.papers.aliases_of(paper_id))
        found = first_available(ctx.fulltext, request)
        now = ctx.now_iso()
        sha256 = hashlib.sha256(found.content).hexdigest() if found else ""
        key = pdf_key(sha256) if found else ""
        if found:
            ctx.blobs.put(key, found.content)
        with ctx.store.atomic():
            current = ctx.store.papers.get_fulltext(paper_id)
            if current and current.pdf_sha256:  # 网络查询期间用户可能已经上传了 PDF
                return
            if found is None:
                ctx.store.papers.save_fulltext(FullTextState(paper_id, "unavailable"), now)
                return
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
        with ctx.store.atomic():
            state = ctx.store.papers.get_fulltext(payload["paper_id"])
            if state is None or not state.pdf_sha256:
                ctx.store.papers.save_fulltext(
                    FullTextState(payload["paper_id"], "failed"), ctx.now_iso()
                )


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
        parsed = ctx.parser.parse(pdf, state.pdf_sha256)
        document = parsed.document
        key, stored = save_document(ctx, document)
        raw_key = save_parser_raw(ctx, document, parsed.raw)
        chunked = load_chunks(ctx, key)
        now = ctx.now_iso()
        with ctx.store.atomic():
            ctx.store.papers.add_artifact(
                Artifact(state.pdf_sha256, "doc", key, len(stored), document.parser_id, None), now
            )
            if raw_key:
                ctx.store.papers.add_artifact(
                    Artifact(
                        state.pdf_sha256,
                        "parser_raw",
                        raw_key[0],
                        raw_key[1],
                        document.parser_id,
                        None,
                    ),
                    now,
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
