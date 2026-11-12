"""本地全文和个人记忆检索。索引是事实记录的可重建投影。"""

from dataclasses import dataclass
from typing import Literal, Protocol

from litgleaner.domain.document import Block

SearchKind = Literal["paper", "fulltext", "note", "highlight", "extraction", "qa", "concept"]
Authorship = Literal["author", "user", "model"]


@dataclass(frozen=True)
class SearchHit:
    kind: SearchKind
    id: str
    paper_id: str
    title: str
    text: str
    page: int | None
    authorship: Authorship


@dataclass(frozen=True)
class QASearchHit:
    kind: SearchKind
    id: str
    paper_id: str
    text: str
    page: int | None
    authorship: Authorship
    source_version: str
    rank: float


@dataclass(frozen=True)
class SearchDocument:
    paper_id: str
    doc_key: str


class SearchRepo(Protocol):
    def find(self, query: str, limit: int) -> tuple[SearchHit, ...]: ...
    def qa_hits(
        self, query: str, paper_ids: tuple[str, ...], limit: int = 64
    ) -> tuple[QASearchHit, ...]: ...
    def pending_documents(self) -> tuple[SearchDocument, ...]: ...
    def replace_document(self, paper_id: str, doc_key: str, blocks: tuple[Block, ...]) -> None: ...
