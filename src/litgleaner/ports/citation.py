"""引文事实查询，不评判论文间的语义关系。"""

from typing import Protocol

from litgleaner.domain.citation import Citation


class CitationRepo(Protocol):
    def add(self, citation: Citation) -> bool: ...
    def outgoing(self, paper_id: str) -> tuple[Citation, ...]: ...
    def incoming(self, paper_id: str) -> tuple[Citation, ...]: ...
