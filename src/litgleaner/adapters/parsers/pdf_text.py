"""直接读取 PDF 文字层。不需要网络和额度，用作对照组和降级方案。

拿不到表格、公式结构和图片位置，扫描件没有文字层时会被标记为低质量。
"""

import io
import re
import statistics

from pypdf import PdfReader

from litgleaner.domain.document import DOCUMENT_SCHEMA_VERSION, Block, NormalizedDocument
from litgleaner.domain.errors import PermanentError
from litgleaner.ports.parser import ParseResult

_NUMBERED = re.compile(r"^(?:[IVX]+\.|[A-Z]\.|\d+(?:\.\d+)*\.?)\s+\S")
_KNOWN = re.compile(
    r"^(abstract|references|bibliography|acknowledg(e)?ments?|conclusions?|introduction"
    r"|参考文献|致谢|摘要|结论|引言)$",
    re.IGNORECASE,
)
_RUN_IN_START = re.compile(r"^(abstract\b|index terms|key\s*words?|摘\s*要|关键词)", re.IGNORECASE)
_LEADING_NUMBER = re.compile(r"^(\d+)")
_MAX_SECTION_NUMBER = 50  # “2003.” “2262 期刊名” 是年份和页眉，不是章节号
_HEADING_MAX_CHARS = 80
DEFAULT_MAX_PAGES = 500
_SHORT_LINE_RATIO = 0.7
_MIN_DOCUMENT_CHARS = 500


def _is_heading(line: str) -> bool:
    """编号开头、较短、不以句号结尾，或者是常见章节名。"""
    stripped = line.strip()
    if _KNOWN.match(stripped):
        return True
    number = _LEADING_NUMBER.match(stripped)
    if number and int(number.group(1)) > _MAX_SECTION_NUMBER:
        return False
    return (
        bool(_NUMBERED.match(stripped))
        and len(stripped) <= _HEADING_MAX_CHARS
        and not stripped.endswith((".", "。"))
    )


def _join(lines: list[str]) -> str:
    text = ""
    for line in lines:
        if text.endswith("-") and line[:1].islower():
            text = text[:-1] + line
        else:
            text = f"{text} {line}" if text else line
    return text


def _page_blocks(text: str, page: int) -> list[Block]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    typical = statistics.median(len(line) for line in lines) if lines else 0
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append(Block("text", _join(paragraph), page))
            paragraph.clear()

    for line in lines:
        if _is_heading(line):
            flush()
            blocks.append(Block("heading", line, page))
            continue
        if _RUN_IN_START.match(line):
            flush()  # 摘要常和标题、作者挤在同一段，要单独成段才能被识别
        paragraph.append(line)
        ends_sentence = line.endswith((".", "。", "?", "!", ":"))
        if ends_sentence and len(line) < typical * _SHORT_LINE_RATIO:
            flush()
    flush()
    return blocks


class PdfTextParser:
    parser_id = "pdf_text@pypdf"

    def __init__(self, max_pages: int = DEFAULT_MAX_PAGES) -> None:
        self._max_pages = max_pages

    def _pages(self, pdf: bytes) -> list[str]:
        """PDF 是不可信输入，读取中的任何异常都说明这个文件处理不了，重试也没用。"""
        try:
            reader = PdfReader(io.BytesIO(pdf))
            if len(reader.pages) > self._max_pages:
                raise PermanentError(f"PDF 超过 {self._max_pages} 页，不处理")
            return [page.extract_text() or "" for page in reader.pages]
        except PermanentError:
            raise
        except Exception as error:
            raise PermanentError(f"无法读取 PDF：{type(error).__name__}") from error

    def parse(self, pdf: bytes, sha256: str) -> ParseResult:
        pages = self._pages(pdf)
        blocks = tuple(
            block for index, text in enumerate(pages) for block in _page_blocks(text, index)
        )
        characters = sum(len(block.text) for block in blocks)
        document = NormalizedDocument(
            schema_version=DOCUMENT_SCHEMA_VERSION,
            source_pdf_sha256=sha256,
            parser_id=self.parser_id,
            title=None,
            blocks=blocks,
            front_matter=(),
            quality="ok" if characters >= _MIN_DOCUMENT_CHARS else "degraded",
        )
        return ParseResult(document, raw=None)
