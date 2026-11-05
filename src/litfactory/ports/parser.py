"""PDF 解析接口。输出必须是统一文档格式。"""

from typing import Protocol

from litfactory.domain.document import NormalizedDocument


class Parser(Protocol):
    parser_id: str

    def parse(self, pdf: bytes, sha256: str) -> NormalizedDocument: ...
