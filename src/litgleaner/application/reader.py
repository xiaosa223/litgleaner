"""领读器的查询和操作。本地 HTTP 接口只做协议适配，逻辑都在这里，命令行和 MCP 共用。"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC
from typing import Any

from litgleaner.application.cards import card_from_dict, tier_label
from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_document, pdf_key
from litgleaner.application.stages.archive import assessment_entries
from litgleaner.application.topics import active_topics
from litgleaner.application.translation import translatable_indices
from litgleaner.domain.document import body_start
from litgleaner.domain.errors import NotFound
from litgleaner.domain.paper import Paper
from litgleaner.domain.reading import (
    Annotation,
    BBox,
    NewAnnotation,
    ReadingState,
    ReadingStatus,
)
from litgleaner.domain.topic import TopicGoal

_RECENT_LIMIT = 200


@dataclass(frozen=True)
class InboxItem:
    paper_id: str
    title: str
    authors: tuple[str, ...]
    venue: str | None
    published_at: str | None
    doi: str | None
    score: int | None  # 没推送过的论文（例如手动加入待读）没有分数和主题
    tier_label: str | None
    provisional: bool
    summary: str | None
    topic_id: str | None
    topic_name: str | None
    pushed_at: str | None
    reading_status: ReadingStatus | None


@dataclass(frozen=True)
class TopicProfile:
    id: str
    name: str
    goals: tuple[TopicGoal, ...]


@dataclass(frozen=True)
class Inbox:
    recommended: tuple[InboxItem, ...]  # 最近推荐，新的在前，每篇只出现一次
    queued: tuple[InboxItem, ...]  # 待读队列
    reading: tuple[InboxItem, ...]  # 在读，最近读过的在前
    topics: tuple[TopicProfile, ...]  # 启用中的研究画像，首页顶部显示


@dataclass(frozen=True)
class PaperView:
    paper_id: str
    title: str
    abstract: str | None
    authors: tuple[str, ...]
    venue: str | None
    published_at: str | None
    doi: str | None
    identifiers: Mapping[str, str]  # 全部标识符，按类型
    fulltext_status: str | None
    parse_quality: str | None
    has_pdf: bool
    has_document: bool
    extraction: Mapping[str, Any] | None
    assessments: Mapping[str, Mapping[str, Any]]
    reading: ReadingState | None
    annotations: tuple[Annotation, ...]


@dataclass(frozen=True)
class BlockView:
    index: int
    kind: str
    text: str
    page: int
    bbox: BBox | None
    caption: str | None
    translatable: bool  # 正文段落且不是中文时可以按段翻译


@dataclass(frozen=True)
class DocumentView:
    quality: str
    body_start: int  # 正文从这一块开始（第一个摘要标记），段落视图从这里显示
    blocks: tuple[BlockView, ...]


def _paper(ctx: AppContext, paper_id: str) -> Paper:
    paper = ctx.store.papers.get(paper_id)
    if paper is None:
        raise NotFound(f"论文不存在：{paper_id}")
    return paper


def _inbox_item(
    ctx: AppContext, topic_id: str, pushed_at: str, data: Mapping[str, Any]
) -> InboxItem:
    card = card_from_dict(data)
    state = ctx.store.reader.get_state(card.paper_id)
    topic = ctx.topics.get(topic_id)
    return InboxItem(
        paper_id=card.paper_id,
        title=card.title,
        authors=card.authors,
        venue=card.venue,
        published_at=card.published_at,
        doi=card.doi,
        score=card.score,
        tier_label=card.tier_label or card.tier,
        provisional=card.provisional,
        summary=card.summary,
        topic_id=topic_id,
        topic_name=topic.name if topic else topic_id,
        pushed_at=pushed_at,
        reading_status=state.status if state else None,
    )


def _unpushed_item(ctx: AppContext, paper_id: str, status: ReadingStatus) -> InboxItem:
    """没推送过、但在待读或在读里的论文：只有元数据和提取结果。"""
    paper = _paper(ctx, paper_id)
    aliases = {alias.scheme: alias.value for alias in ctx.store.papers.aliases_of(paper_id)}
    extraction = ctx.store.papers.get_extraction(paper_id)
    summary = extraction.data.get("summary") if extraction else None
    return InboxItem(
        paper_id=paper.id,
        title=paper.title,
        authors=paper.authors,
        venue=paper.venue,
        published_at=paper.published_at,
        doi=aliases.get("doi"),
        score=None,
        tier_label=None,
        provisional=False,
        summary=summary if isinstance(summary, str) else None,
        topic_id=None,
        topic_name=None,
        pushed_at=None,
        reading_status=status,
    )


def inbox(ctx: AppContext) -> Inbox:
    """首页：最近推荐和待读队列。推荐内容取推送时的卡片，和渠道里看到的一致。"""
    latest: dict[str, InboxItem] = {}
    for push in ctx.store.outputs.recent_pushes(_RECENT_LIMIT):
        if push.paper_id not in latest:
            latest[push.paper_id] = _inbox_item(ctx, push.topic_id, push.created_at, push.card)

    def listed(status: ReadingStatus) -> tuple[InboxItem, ...]:
        return tuple(
            latest.get(paper_id) or _unpushed_item(ctx, paper_id, status)
            for paper_id in ctx.store.reader.papers_with_status(status)
        )

    topics = tuple(
        TopicProfile(topic.id, topic.name, topic.goals) for topic in active_topics(ctx).values()
    )
    return Inbox(tuple(latest.values()), listed("queued"), listed("reading"), topics)


def month_cost_usd(ctx: AppContext) -> float:
    """本月（按配置的时区）模型花费。"""
    local = ctx.clock().astimezone(ctx.settings.timezone)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return ctx.store.runs.llm_cost_since(start.astimezone(UTC).isoformat())


def _labeled(
    ctx: AppContext, entries: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """给界面显示用：加上主题名称和等级名称。主题已删除时保留编号。"""
    labeled: dict[str, dict[str, Any]] = {}
    for topic_id, entry in entries.items():
        topic = ctx.topics.get(topic_id)
        rubric = ctx.rubrics.get(topic.rubric) if topic else None
        label = tier_label(rubric, entry["tier"]) if rubric else entry["tier"]
        labeled[topic_id] = {
            **entry,
            "topic_name": topic.name if topic else topic_id,
            "tier_label": label,
        }
    return labeled


def paper_view(ctx: AppContext, paper_id: str) -> PaperView:
    paper = _paper(ctx, paper_id)
    aliases = {alias.scheme: alias.value for alias in ctx.store.papers.aliases_of(paper_id)}
    fulltext = ctx.store.papers.get_fulltext(paper_id)
    extraction = ctx.store.papers.get_extraction(paper_id)
    return PaperView(
        paper_id=paper.id,
        title=paper.title,
        abstract=paper.abstract,
        authors=paper.authors,
        venue=paper.venue,
        published_at=paper.published_at,
        doi=aliases.get("doi"),
        identifiers=aliases,
        fulltext_status=fulltext.status if fulltext else None,
        parse_quality=fulltext.quality if fulltext else None,
        has_pdf=bool(fulltext and fulltext.pdf_sha256),
        has_document=bool(fulltext and fulltext.doc_key),
        extraction=extraction.data if extraction else None,
        assessments=_labeled(ctx, assessment_entries(ctx, paper_id)),
        reading=ctx.store.reader.get_state(paper_id),
        annotations=ctx.store.reader.annotations(paper_id),
    )


def document_view(ctx: AppContext, paper_id: str) -> DocumentView | None:
    """段落视图的内容。没有解析结果时返回 None，界面只提供原版视图或摘要。"""
    _paper(ctx, paper_id)
    fulltext = ctx.store.papers.get_fulltext(paper_id)
    if fulltext is None or fulltext.doc_key is None:
        return None
    document = load_document(ctx, fulltext.doc_key)
    allowed = translatable_indices(document.blocks)
    blocks = tuple(
        BlockView(index, b.kind, b.text, b.page, b.bbox, b.caption, index in allowed)
        for index, b in enumerate(document.blocks)
    )
    return DocumentView(document.quality, body_start(document.blocks), blocks)


def pdf_content(ctx: AppContext, paper_id: str) -> bytes:
    _paper(ctx, paper_id)
    fulltext = ctx.store.papers.get_fulltext(paper_id)
    if fulltext is None or fulltext.pdf_sha256 is None:
        raise NotFound(f"这篇论文没有 PDF：{paper_id}")
    return ctx.blobs.get(pdf_key(fulltext.pdf_sha256))


def set_reading_state(
    ctx: AppContext, paper_id: str, status: ReadingStatus, position: dict[str, Any] | None
) -> None:
    _paper(ctx, paper_id)
    ctx.store.reader.set_state(paper_id, status, position, ctx.now_iso())


def add_annotation(ctx: AppContext, item: NewAnnotation) -> Annotation:
    _paper(ctx, item.paper_id)
    return ctx.store.reader.add_annotation(item, ctx.now_iso())


def edit_annotation(ctx: AppContext, annotation_id: str, body: str | None) -> None:
    if not ctx.store.reader.update_annotation(annotation_id, body, ctx.now_iso()):
        raise NotFound(f"批注不存在：{annotation_id}")


def remove_annotation(ctx: AppContext, annotation_id: str) -> None:
    if not ctx.store.reader.delete_annotation(annotation_id, ctx.now_iso()):
        raise NotFound(f"批注不存在：{annotation_id}")
