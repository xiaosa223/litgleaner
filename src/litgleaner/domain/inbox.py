"""添加论文（第 12.8 节）：认出用户贴进来的 DOI、arXiv 编号或链接，或从 PDF 首页文字里找标识符。

只做格式识别，元数据由信号源给出，不经过模型（R16）。PDF 首页的做法参照 Zotero“从 PDF 获取元数据”
和 pdf2doi：先找 DOI，再找 arXiv 编号；只看首页前段，免得把参考文献里的 DOI 当成本文的。
"""

import re
from dataclasses import dataclass
from typing import Literal

from litgleaner.domain.identity import normalize_arxiv_id, normalize_doi
from litgleaner.domain.query import IdentifierLookup

Kind = Literal["doi", "arxiv", "url"]
InboxKind = Literal["doi", "arxiv", "url", "pdf"]
InboxStatus = Literal["pending", "done", "failed"]

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.IGNORECASE)
_ARXIV_URL = re.compile(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE)
_ARXIV_ID = re.compile(r"^(?:arxiv:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?)$", re.IGNORECASE)
_ARXIV_STAMP = re.compile(r"arxiv:\s*(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
# arXiv 官方页边戳“arXiv:编号v版本 [分类] 日期”：文字层常排在首页最后，按全页查找
_ARXIV_MARGIN = re.compile(r"arxiv:(\d{4}\.\d{4,5})v\d+\s+\[[a-z][a-z.\-]*\]", re.IGNORECASE)
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
FIRST_PAGE_CHARS = 3000  # 首页前段：标题、作者、摘要、页眉页脚里的 DOI 和 arXiv 戳


@dataclass(frozen=True)
class Identified:
    kind: Kind
    value: str


def _doi(text: str) -> Identified | None:
    found = _DOI.search(text)
    raw = found.group(1).rstrip(".,;:>") if found else ""
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while raw.endswith(closing) and raw.count(closing) > raw.count(opening):
            raw = raw[:-1]
    value = normalize_doi(raw)
    return Identified("doi", value) if value else None


def _arxiv(raw: str) -> Identified | None:
    value = normalize_arxiv_id(raw)
    return Identified("arxiv", value) if value else None


def identify(text: str) -> Identified | None:
    """用户贴进来的一段文字。认不出时返回 None。"""
    stripped = text.strip()
    if not stripped:
        return None
    if doi := _doi(stripped):
        return doi
    found = _ARXIV_URL.search(stripped) or _ARXIV_ID.match(stripped)
    if found:
        return _arxiv(found.group(1))
    return Identified("url", stripped) if _URL.match(stripped) else None


def find_identifier(first_page: str) -> Identified | None:
    """从 PDF 首页文字里找本文的标识符。"""
    head = first_page[:FIRST_PAGE_CHARS]
    if doi := _doi(head):
        return doi
    stamp = _ARXIV_STAMP.search(head) or _ARXIV_MARGIN.search(first_page)
    return _arxiv(stamp.group(1)) if stamp else None


@dataclass(frozen=True)
class InboxItem:
    """用户添加的一篇论文：贴进来的标识符或上传的 PDF，以及对上的论文。"""

    id: str
    kind: InboxKind
    value: str  # 标识符、链接或文件名
    status: InboxStatus
    created_at: str
    pdf_sha256: str | None = None
    paper_id: str | None = None
    error: str | None = None


def lookup_query(found: Identified) -> IdentifierLookup:
    """标识符对应的信号源查询。"""
    return IdentifierLookup(
        doi=found.value if found.kind == "doi" else None,
        arxiv_id=found.value if found.kind == "arxiv" else None,
    )


# 给用户看的提示：认得出哪些标识符
ACCEPTED = "DOI（10. 开头）或 arXiv 编号（如 1706.03762）"
