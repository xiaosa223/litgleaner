"""文库查询：只有用户接触过的论文进入文库，不依赖最近推荐的窗口。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

LibraryStatus = Literal["new", "queued", "reading", "read", "abandoned"]
StatusFilter = Literal["all", "new", "queued", "reading", "read", "abandoned"]


@dataclass(frozen=True)
class CatalogQuery:
    status: StatusFilter = "all"
    topic_id: str | None = None
    q: str = ""
    limit: int = 50
    offset: int = 0


@dataclass(frozen=True)
class CatalogEntry:
    paper_id: str
    topic_ids: tuple[str, ...]
    card: Mapping[str, Any] | None = None
    card_topic_id: str | None = None
    pushed_at: str | None = None


@dataclass(frozen=True)
class CatalogResult:
    entries: tuple[CatalogEntry, ...]
    total: int
    counts: Mapping[str, int]


class CatalogRepo(Protocol):
    def page(self, query: CatalogQuery) -> CatalogResult: ...
