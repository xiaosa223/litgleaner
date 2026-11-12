"""添加论文（第 12.8 节）：贴标识符，或上传 PDF。

- 元数据只来自信号源（按标识符查询），不经过模型；
  只有 PDF、找不到标识符时用文件名建条目，标明“元数据待补”。
- 对上的论文进入文库（待读），取全文不要求通过初筛（见 AcquireStage 的规划）；
  解析、提取照常由流水线完成。
- 按标识符查元数据在请求里同步完成，用户要等它才能打开论文；
  查询出错（网络、限流）时记为失败并说明原因。
"""

import hashlib
import logging
from pathlib import PurePath

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.application.documents import pdf_key
from litgleaner.application.memory_admission import register
from litgleaner.application.stages.discover import resolve_record
from litgleaner.domain.errors import InvalidRequest, LitGleanerError
from litgleaner.domain.identity import Alias
from litgleaner.domain.inbox import (
    ACCEPTED,
    Identified,
    InboxItem,
    InboxKind,
    InboxStatus,
    identify,
    lookup_query,
)
from litgleaner.domain.paper import SourceRecord
from litgleaner.ports.store import Artifact, FullTextState

logger = logging.getLogger(__name__)
MAX_PDF_BYTES = 100 * 1024 * 1024
USER_PROVIDER = "user"
USER_LICENSE = "user_supplied"
_PDF_MAGIC = b"%PDF-"
_NO_METADATA = f"元数据待补：PDF 首页没有找到 {ACCEPTED}，暂用文件名作标题"


def _lookup(ctx: AppContext, found: Identified) -> tuple[str | None, str | None]:
    """按标识符向信号源查元数据，返回（论文 ID，失败原因）。"""
    query = lookup_query(found)
    local = ctx.store.papers.find_ids_by_aliases((Alias(found.kind, found.value),))
    if len(local) == 1:
        return next(iter(local)), None
    errors: list[str] = []
    for source in ctx.sources.values():
        if type(query) not in source.capabilities:
            continue
        try:
            result = source.fetch(query, None)
        except LitGleanerError as error:
            logger.warning("信号源 %s 按标识符查询失败：%s", source.name, error)
            errors.append(f"{source.name}：{error}")
            continue
        if result.records:
            now = ctx.now_iso()
            with ctx.store.atomic():
                paper_id = resolve_record(ctx.store.papers, result.records[0], now)
                ctx.store.papers.record_sighting(paper_id, source.name, query.hash, None, now)
            return paper_id, None
    if errors:
        return None, "查询数据源出错，稍后再试：" + "；".join(errors)
    return None, f"已启用的数据源里查不到 {found.value}"  # 调用方据此提示


def _record(ctx: AppContext, kind: InboxKind, value: str, sha256: str | None) -> InboxItem:
    item = InboxItem(str(ULID()), kind, value, "pending", ctx.now_iso(), pdf_sha256=sha256)
    ctx.store.inbox.add(item)
    return item


def _finish(ctx: AppContext, item: InboxItem, paper_id: str | None, error: str | None) -> InboxItem:
    """对上论文就算完成（error 可以是“元数据待补”这样的说明）；没对上记为失败。"""
    status: InboxStatus = "done" if paper_id else "failed"
    now = ctx.now_iso()
    with ctx.store.atomic():
        ctx.store.inbox.finish(item.id, status, paper_id, error, now)
        if paper_id and ctx.store.reader.get_state(paper_id) is None:
            ctx.store.reader.set_state(paper_id, "queued", None, now)  # 进入文库：待读
        if paper_id:
            register(ctx, paper_id)
    found = ctx.store.inbox.get(item.id)
    if found is None:
        raise RuntimeError(f"收件箱条目写入后读不到：{item.id}")
    return found


def add_identifier(ctx: AppContext, text: str) -> InboxItem:
    found = identify(text)
    if found is None:
        raise InvalidRequest(f"认不出这是什么：请贴 {ACCEPTED}，或拖入 PDF")
    if found.kind == "url":
        raise InvalidRequest(f"暂时只认得出含 {ACCEPTED} 的链接；其他链接请下载 PDF 后拖入")
    item = _record(ctx, found.kind, found.value, None)
    paper_id, error = _lookup(ctx, found)
    return _finish(ctx, item, paper_id, error)


def _check_pdf(content: bytes) -> None:
    if len(content) > MAX_PDF_BYTES:
        raise InvalidRequest(f"文件超过 {MAX_PDF_BYTES // (1024 * 1024)} MB")
    if not content.startswith(_PDF_MAGIC):
        raise InvalidRequest("只能添加 PDF 文件：文件头不是 %PDF-")


def _title_from_file(file_name: str) -> str:
    stem = PurePath(file_name.replace("\\", "/")).stem.strip()
    return stem or "未命名论文"


def _paper_for_pdf(
    ctx: AppContext, file_name: str, identifier: str | None
) -> tuple[str | None, str | None]:
    found = identify(identifier) if identifier else None
    note = _NO_METADATA
    if found is not None and found.kind != "url":
        paper_id, error = _lookup(ctx, found)
        if paper_id:
            return paper_id, None
        note = f"元数据待补：首页找到 {found.value}，但{error}；暂用文件名作标题"
    return None, note


def add_pdf(ctx: AppContext, file_name: str, content: bytes, identifier: str | None) -> InboxItem:
    """用户上传的 PDF。identifier 是前端从 PDF 首页文字里找到的标识符，可以为空。"""
    _check_pdf(content)
    sha256 = hashlib.sha256(content).hexdigest()
    key = pdf_key(sha256)
    ctx.blobs.put(key, content)
    item = _record(ctx, "pdf", file_name[:255], sha256)
    if (existing := ctx.store.papers.paper_with_pdf(sha256)) is not None:
        return _finish(ctx, item, existing, None)
    # 有标识符的网络查询在事务外完成；无标识符时的建条目与 PDF 判重在同一事务里。
    paper_id, note = _paper_for_pdf(ctx, file_name, identifier)
    now = ctx.now_iso()
    with ctx.store.atomic():
        same_file = ctx.store.papers.paper_with_pdf(sha256)
        if same_file is not None:
            return _finish(ctx, item, same_file, None)
        if paper_id is None:
            record = SourceRecord(title=_title_from_file(file_name))
            paper_id = resolve_record(ctx.store.papers, record, now)
        artifact = Artifact(sha256, "pdf", key, len(content), USER_PROVIDER, USER_LICENSE)
        ctx.store.papers.add_artifact(artifact, now)
        current = ctx.store.papers.get_fulltext(paper_id)
        if current is None or current.pdf_sha256 is None:  # 已有 PDF 时不替换；取不到时用上传的
            state = FullTextState(
                paper_id,
                "available",
                pdf_sha256=sha256,
                provider=USER_PROVIDER,
                license_basis=USER_LICENSE,
            )
            ctx.store.papers.save_fulltext(state, now)
    return _finish(ctx, item, paper_id, note)
