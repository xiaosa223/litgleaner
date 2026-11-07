"""假解析器：生成结构固定的文档。标题含 BadParse 时模拟外部格式变化。"""

import json

from litgleaner.domain.document import DOCUMENT_SCHEMA_VERSION, Block, NormalizedDocument
from litgleaner.domain.errors import SchemaChanged
from litgleaner.ports.parser import ParseResult


class FakeParser:
    parser_id = "fake_parser@1"

    def __init__(self) -> None:
        self.fail_next: list[Exception] = []
        self.raw_output: bytes | None = None
        self.calls = 0

    def parse(self, pdf: bytes, sha256: str) -> ParseResult:
        self.calls += 1
        if self.fail_next:
            raise self.fail_next.pop(0)
        return ParseResult(self._document(pdf, sha256), self.raw_output)

    def _document(self, pdf: bytes, sha256: str) -> NormalizedDocument:
        title = json.loads(pdf.split(b"\n", 1)[1])["title"]
        if "BadParse" in title:
            raise SchemaChanged("模拟解析结果结构变化")
        blocks = (
            Block("text", f"Abstract—Study of {title}.", 0),
            Block("heading", "I. Introduction", 0),
            Block("text", "We study the problem.", 0),
            Block("heading", "II. Conclusion", 1),
            Block("text", "We found it works.", 1),
        )
        return NormalizedDocument(
            schema_version=DOCUMENT_SCHEMA_VERSION,
            source_pdf_sha256=sha256,
            parser_id=self.parser_id,
            title=title,
            blocks=blocks,
            front_matter=(),
            quality="ok",
        )
