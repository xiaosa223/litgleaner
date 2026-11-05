"""全文获取接口。发现和取全文是两个独立的职责。"""

from dataclasses import dataclass
from typing import Literal, Protocol

from litfactory.domain.identity import Alias

LicenseBasis = Literal["open", "subscribed", "user_supplied"]


@dataclass(frozen=True)
class FullTextRequest:
    paper_id: str
    title: str
    aliases: tuple[Alias, ...]


@dataclass(frozen=True)
class FullText:
    content: bytes
    license_basis: LicenseBasis
    provider: str


class FullTextProvider(Protocol):
    name: str

    def fetch(self, request: FullTextRequest) -> FullText | None: ...
