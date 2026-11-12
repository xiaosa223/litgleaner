"""问答实际发送的有身份连续来源；规范记录的版本由应用层核验。"""

from dataclasses import dataclass
from typing import Literal

CONTRACT = "qa-memory-v1"


@dataclass(frozen=True)
class QASource:
    id: str
    paper_id: str
    kind: Literal["author", "note", "question"]
    text: str
    page: int | None = None
    doc_key: str | None = None
    source_id: str | None = None
    version: str = ""
