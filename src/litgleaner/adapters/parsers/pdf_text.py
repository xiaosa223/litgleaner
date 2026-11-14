"""直接读取 PDF 文字层。不需要网络和额度，用作对照组和降级方案。

拿不到表格、公式结构和图片位置，扫描件没有文字层时会被标记为低质量。
段落坐标按文字层估算（见 pdf_layout），只用于原版视图定位，不改变文字和切分。
"""

import io
import re
import statistics
import unicodedata

from pypdf import PdfReader

from litgleaner.adapters.parsers.pdf_layout import Box, block_bbox, page_lines
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
_NUMBER_PREFIX = re.compile(r"^(?:[IVX]+\.|[A-Z]\.|\d+(?:\.\d+)*\.?)\s*")
_MAX_SECTION_NUMBER = 50  # “2003.” “2262 期刊名” 是年份和页眉，不是章节号
_HEADING_MAX_CHARS = 80
DEFAULT_MAX_PAGES = 500
_SHORT_LINE_RATIO = 0.7
_MIN_DOCUMENT_CHARS = 500
_TITLE_SCAN_LINES = 15
_TITLE_HEIGHT_RATIO = 0.85  # 与最大行高相差不超过这个比例的相邻行算作同一个标题


def _looks_like_data(line: str) -> bool:
    """图表里的公式、坐标刻度不是章节标题：含等号、波浪号，或数字占了大半。"""
    if any(mark in line for mark in "~∼≈") or line.count("=") >= 2:
        return True  # 单个等号可以出现在标题里（“Case Study on L = 1”）
    visible = [c for c in line if not c.isspace()]
    return bool(visible) and sum(c.isdigit() for c in visible) / len(visible) > 0.5


_AFFILIATION = re.compile(
    r"\b(department|university|institute|school of|laboratory|college)\b", re.I
)
_AFFILIATION_ZH = re.compile(r"(大学|学院|实验室|研究所|研究院|研究中心)")
_SENTENCE_WORDS = 8
_ROMAN = re.compile(r"^[IVX]+\.\s")
_LETTER = re.compile(r"^[A-Z]\.\s")
_INITIAL = re.compile(r"\b[A-Z]\.\s")
_SURNAME = re.compile(r"^[A-Z][a-z]+\.\s+\S")  # “N. Elhadad. Intelligible …”


def _looks_like_prose(line: str) -> bool:
    """编号后面接的是正文句子（小写开头、带逗号的长句、括号收尾）或作者单位，不是章节名。"""
    body = _NUMBER_PREFIX.sub("", line, count=1)
    if not body or body[0].islower() or _AFFILIATION.search(body) or _AFFILIATION_ZH.search(body):
        return True
    if _LETTER.match(line) and (("," in body and _INITIAL.search(body)) or _SURNAME.match(body)):
        return True  # “P. Simard, and J. Suh. …”：参考文献里的作者名，不是 P 小节
    if _ROMAN.match(line):  # IEEE 一级标题全大写；小写为主的是被折到行首的“Table III”之类
        letters = [c for c in body if c.isalpha() and c.isascii()]
        if letters and sum(c.islower() for c in letters) > len(letters) / 2:
            return True
    return ("," in body or ")" in body) and len(body.split()) > _SENTENCE_WORDS


def _is_heading(line: str) -> bool:
    """编号开头、较短、不以句号结尾，或者是常见章节名。"""
    stripped = line.strip()
    if _KNOWN.match(stripped):
        return True
    if _looks_like_data(stripped) or _looks_like_prose(stripped):
        return False
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


def _page_blocks(lines: list[tuple[str, Box | None]], page: int) -> list[Block]:
    typical = statistics.median(len(line) for line, _ in lines) if lines else 0
    blocks: list[Block] = []
    paragraph: list[str] = []
    boxes: list[Box | None] = []

    def flush() -> None:
        if paragraph:
            blocks.append(Block("text", _join(paragraph), page, block_bbox(boxes)))
            paragraph.clear()
            boxes.clear()

    for line, box in lines:
        if _is_heading(line):
            flush()
            blocks.append(Block("heading", line, page, block_bbox([box])))
            continue
        if _RUN_IN_START.match(line):
            flush()  # 摘要常和标题、作者挤在同一段，要单独成段才能被识别
        paragraph.append(line)
        boxes.append(box)
        ends_sentence = line.endswith((".", "。", "?", "!", ":"))
        if ends_sentence and len(line) < typical * _SHORT_LINE_RATIO:
            flush()
    flush()
    return blocks


def _title(lines: list[tuple[str, Box | None]]) -> str | None:
    """首页最大字号的相邻几行就是标题；行高（千分比）近似字号。拿不准时不给。"""
    head = [(text, box) for text, box in lines[:_TITLE_SCAN_LINES] if box is not None]
    heights = [box[3] - box[1] for _, box in head]
    if not heights:
        return None
    tallest = max(heights)
    if sorted(heights)[len(heights) // 2] * 1.2 >= tallest:
        return None  # 首页没有明显更大的字，可能没有标题行
    picked: list[str] = []
    for (text, _), height in zip(head, heights, strict=True):
        if height >= tallest * _TITLE_HEIGHT_RATIO:
            picked.append(text)
        elif picked:
            break
    title = " ".join(unicodedata.normalize("NFKC", " ".join(picked)).split())  # ﬁ 等连字
    if not 8 <= len(title) <= 300 or _KNOWN.match(title) or _RUN_IN_START.match(title):
        return None
    return title


class PdfTextParser:
    parser_id = "pdf_text@pypdf"

    def __init__(self, max_pages: int = DEFAULT_MAX_PAGES) -> None:
        self._max_pages = max_pages

    def _pages(self, pdf: bytes) -> list[list[tuple[str, Box | None]]]:
        """PDF 是不可信输入，读取中的任何异常都说明这个文件处理不了，重试也没用。"""
        try:
            reader = PdfReader(io.BytesIO(pdf))
            if len(reader.pages) > self._max_pages:
                raise PermanentError(f"PDF 超过 {self._max_pages} 页，不处理")
            return [page_lines(page) for page in reader.pages]
        except PermanentError:
            raise
        except Exception as error:
            raise PermanentError(f"无法读取 PDF：{type(error).__name__}") from error

    def parse(self, pdf: bytes, sha256: str) -> ParseResult:
        pages = self._pages(pdf)
        blocks = tuple(
            block for index, lines in enumerate(pages) for block in _page_blocks(lines, index)
        )
        characters = sum(len(block.text) for block in blocks)
        document = NormalizedDocument(
            schema_version=DOCUMENT_SCHEMA_VERSION,
            source_pdf_sha256=sha256,
            parser_id=self.parser_id,
            title=_title(pages[0]) if pages else None,
            blocks=blocks,
            front_matter=(),
            quality="ok" if characters >= _MIN_DOCUMENT_CHARS else "degraded",
        )
        return ParseResult(document, raw=None)
