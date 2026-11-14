"""在本机抽取上传文档的文字（ADR 0012）：不走解析服务，不消耗额度。

- PDF：pypdf 读文字层；扫描件没有文字层时报 document_no_text，不静默降级。
- DOCX：标准库读 zip 里的 word/document.xml，按段落换行；限制解压大小，拒绝实体声明。
- Markdown / TXT：UTF-8（含 BOM），失败时试 GB18030。
"""

import io
import logging
import re
import zipfile
from xml.etree import ElementTree

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from litgleaner.domain.errors import InvalidRequest

log = logging.getLogger(__name__)

MAX_XML_BYTES = 10 * 1024 * 1024
MAX_CHARS = 200_000  # 推断画像只用前 1.2 万 token 左右，读够就停
MAX_PAGES = 200
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_TEXT_SUFFIXES = (".md", ".markdown", ".txt", ".text")
_BINARY = re.compile(rb"[\x00-\x08\x0e-\x1f]")


class LocalDocumentText:
    def extract(self, name: str, content: bytes) -> str:
        lowered = name.lower()
        if content.startswith(b"%PDF-"):
            text = _pdf(content)
        elif content.startswith(b"PK\x03\x04"):
            text = _docx(content)
        elif lowered.endswith(_TEXT_SUFFIXES) and not _BINARY.search(content[:4096]):
            text = _plain(content)
        else:
            raise InvalidRequest("document_type_unsupported")
        text = text.strip()
        if not text:
            raise InvalidRequest("document_no_text")
        return text


def _pdf(content: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or "" for page in reader.pages[:MAX_PAGES]]
    except (PyPdfError, ValueError, KeyError, TypeError, OSError, RecursionError) as error:
        log.info("上传的 PDF 无法读取：%s", type(error).__name__)
        raise InvalidRequest("document_unreadable") from None
    return "\n\n".join(page.strip() for page in pages if page.strip())


def _docx(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            try:
                info = archive.getinfo("word/document.xml")
            except KeyError:
                raise InvalidRequest("document_type_unsupported") from None
            if info.file_size > MAX_XML_BYTES:
                raise InvalidRequest("document_too_large")
            with archive.open(info) as handle:
                xml = handle.read(MAX_XML_BYTES + 1)
    except zipfile.BadZipFile:
        raise InvalidRequest("document_unreadable") from None
    if len(xml) > MAX_XML_BYTES:
        raise InvalidRequest("document_too_large")
    if b"<!DOCTYPE" in xml[:4096] or b"<!ENTITY" in xml:
        raise InvalidRequest("document_type_unsupported")
    return _docx_text(xml)


def _docx_text(xml: bytes) -> str:
    """流式解析，段落结束就释放；收集够 MAX_CHARS 个字符后停止，不在内存里建整棵树。"""
    lines: list[str] = []
    parts: list[str] = []
    total = 0
    try:
        for _event, node in ElementTree.iterparse(io.BytesIO(xml), events=("end",)):
            if node.tag == f"{_W}t":
                parts.append(node.text or "")
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag == f"{_W}p":
                line = "".join(parts)
                parts = []
                if line.strip():
                    lines.append(line)
                    total += len(line) + 1
                node.clear()
                if total >= MAX_CHARS:
                    break
    except ElementTree.ParseError:
        raise InvalidRequest("document_unreadable") from None
    return "\n".join(lines)


def _plain(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise InvalidRequest("document_unreadable")
