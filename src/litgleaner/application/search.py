"""全局检索（第 12.8、12.9 节）：元数据、归档全文和个人笔记，共用本地索引。"""

import logging
from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_document
from litgleaner.domain.errors import InvalidRequest
from litgleaner.ports.search import SearchHit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchResults:
    hits: tuple[SearchHit, ...]


def refresh_documents(ctx: AppContext) -> None:
    """旧归档首次检索时补索引；新解析结果在解析阶段写入。无网络和模型调用。"""
    for pending in ctx.store.search.pending_documents():
        try:
            document = load_document(ctx, pending.doc_key)
        except (OSError, ValueError):
            logger.warning("无法读取全文索引的归档：%s", pending.paper_id, exc_info=True)
            continue
        ctx.store.search.replace_document(pending.paper_id, pending.doc_key, document.blocks)


def search(ctx: AppContext, query: str, limit: int = 30) -> SearchResults:
    if len(query) > 500 or not 1 <= limit <= 100:
        raise InvalidRequest("检索参数越界")
    if not query.strip():
        return SearchResults(())
    refresh_documents(ctx)
    return SearchResults(ctx.store.search.find(query, limit))
