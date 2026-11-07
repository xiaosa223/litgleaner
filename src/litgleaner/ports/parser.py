"""PDF 解析接口。输出必须是统一文档格式。"""

from dataclasses import dataclass
from typing import Protocol

from litgleaner.domain.document import NormalizedDocument


@dataclass(frozen=True)
class ParseResult:
    document: NormalizedDocument
    raw: bytes | None  # 解析器的原始输出，短期保留用于排查；不需要时为 None


class Parser(Protocol):
    parser_id: str

    def parse(self, pdf: bytes, sha256: str) -> ParseResult: ...
