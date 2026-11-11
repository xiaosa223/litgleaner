"""文库列表和筛选（第 12.8 节）；读取不会触发模型或重新处理历史论文。"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace

from litgleaner.application.context import AppContext
from litgleaner.application.reader import InboxItem, TopicProfile, library_item
from litgleaner.domain.errors import InvalidRequest
from litgleaner.ports.catalog import CatalogEntry, CatalogQuery

_DEFAULT_QUERY = CatalogQuery()


@dataclass(frozen=True)
class LibraryItem(InboxItem):
    topic_ids: tuple[str, ...]


@dataclass(frozen=True)
class LibraryPage:
    items: tuple[LibraryItem, ...]
    total: int
    counts: Mapping[str, int]
    topics: tuple[TopicProfile, ...]


def _item(ctx: AppContext, entry: CatalogEntry, topic_id: str | None) -> LibraryItem:
    base = library_item(ctx, entry.paper_id, entry.card, entry.pushed_at)
    chosen = topic_id or entry.card_topic_id or next(iter(entry.topic_ids), None)
    topic = ctx.topics.get(chosen) if chosen else None
    base = replace(base, topic_id=chosen, topic_name=topic.name if topic else chosen)
    return LibraryItem(**asdict(base), topic_ids=entry.topic_ids)


def library(ctx: AppContext, query: CatalogQuery = _DEFAULT_QUERY) -> LibraryPage:
    if not 1 <= query.limit <= 200 or query.offset < 0 or len(query.q) > 500:
        raise InvalidRequest("文库查询参数越界")
    if query.status not in {"all", "new", "queued", "reading", "read", "abandoned"}:
        raise InvalidRequest("阅读状态无效")
    result = ctx.store.catalog.page(query)
    topics = tuple(TopicProfile(t.id, t.name, t.goals) for t in ctx.topics.values())
    return LibraryPage(
        tuple(_item(ctx, entry, query.topic_id) for entry in result.entries),
        result.total,
        result.counts,
        topics,
    )
