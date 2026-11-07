"""加入文献库：记录用户的选择，再由 library 阶段写进用户的文献管理软件。

渠道（飞书按钮、命令行）只调用 record_action 写反馈，不直接写文献库：
文献库没开、暂时连不上时，任务留在队列里按退避重试，不会丢。
"""

from ulid import ULID

from litfactory.application.cards import card_from_dict
from litfactory.application.context import AppContext
from litfactory.application.documents import pdf_key
from litfactory.domain.digest import PaperState
from litfactory.domain.errors import ConfigError
from litfactory.domain.library import (
    CardAction,
    LibraryEntry,
    build_library_entry,
    reference_aliases,
)
from litfactory.ports.library import ReferenceLibrary
from litfactory.ports.store import FeedbackRecord, LibraryRecord


def library_job_key(library: str, paper_id: str) -> str:
    return f"library:{library}:{paper_id}"


def record_action(
    ctx: AppContext,
    action: CardAction,
    *,
    channel: str,
    actor: str | None = None,
    external_id: str | None = None,
) -> bool:
    """记下一次选择。论文不存在或这次点击已经记过时返回 False。"""
    if ctx.store.papers.get(action.paper_id) is None:
        return False
    record = FeedbackRecord(
        id=str(ULID()),
        paper_id=action.paper_id,
        channel=channel,
        action=action.action,
        digest_id=action.digest_id,
        actor=actor,
        external_id=external_id,
    )
    return ctx.store.feedback.add(record, ctx.now_iso())


def find_paper(ctx: AppContext, reference: str) -> str:
    """命令行参数可以是论文编号、DOI（含 doi.org 链接）或预印本编号。"""
    value = reference.strip()
    if ctx.store.papers.get(value) is not None:
        return value
    aliases = reference_aliases(value)
    found = ctx.store.papers.find_ids_by_aliases(aliases) if aliases else frozenset()
    if len(found) == 1:
        return next(iter(found))
    if not found:
        raise ConfigError(f"库里没有这篇论文：{value}。只能加入论文工厂已经发现的论文")
    raise ConfigError(f"{value} 对应多篇论文，请改用论文编号：{', '.join(sorted(found))}")


def build_entry(ctx: AppContext, paper_id: str) -> LibraryEntry:
    paper = ctx.store.papers.get(paper_id)
    if paper is None:
        raise ConfigError(f"论文不存在：{paper_id}")
    aliases = {alias.scheme: alias.value for alias in ctx.store.papers.aliases_of(paper_id)}
    latest = ctx.store.feedback.latest_card(paper_id)
    card = card_from_dict(latest[0]) if latest else None
    topic = ctx.topics.get(latest[1]) if latest else None
    return build_library_entry(
        paper, aliases, card, topic.name if topic else None, _pdf(ctx, paper_id)
    )


def _pdf(ctx: AppContext, paper_id: str) -> bytes | None:
    """已经取到的 PDF 一起放进用户自己的文献库。只写本机，不会公开。"""
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is None or not state.pdf_sha256:
        return None
    key = pdf_key(state.pdf_sha256)
    return ctx.blobs.get(key) if ctx.blobs.exists(key) else None


def paper_states(ctx: AppContext, paper_ids: list[str]) -> dict[str, PaperState]:
    """每篇论文在渠道上该显示成什么样。没有启用文献库时，“加入”只算记下了。"""
    states: dict[str, PaperState] = {}
    library = ctx.library.name if ctx.library else None
    for paper_id in paper_ids:
        action = ctx.store.feedback.latest_action(paper_id)
        if action == "dismiss":
            states[paper_id] = "dismissed"
        elif action == "save_to_library":
            saved = library and ctx.store.feedback.get_entry(paper_id, library)
            states[paper_id] = "saved" if saved else "queued"
    return states


def require_library(ctx: AppContext) -> ReferenceLibrary:
    if ctx.library is None:
        raise ConfigError("没有启用文献库：在 config/app.yaml 里配置 library，见 docs/CHANNELS.md")
    return ctx.library


def save_paper(ctx: AppContext, paper_id: str) -> LibraryRecord:
    """写进文献库并记录结果。已经写过的直接返回原记录，不再调用文献库。"""
    library = require_library(ctx)
    existing = ctx.store.feedback.get_entry(paper_id, library.name)
    if existing is not None:
        return existing
    receipt = library.save(build_entry(ctx, paper_id))
    record = LibraryRecord(
        paper_id, library.name, receipt.item_key, receipt.created, receipt.warnings
    )
    ctx.store.feedback.save_entry(record, ctx.now_iso())
    return record
