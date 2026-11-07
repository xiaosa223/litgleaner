"""把 Zotero 条目（Web API 的 data 结构；本地库读出后整理成同样的结构）映射成记录。

DOI 可能写在 DOI 字段，也可能在 extra 里（"DOI: 10.xxx"）；arXiv 编号可能在 URL、
archiveID（"arXiv:2401.01234"）、extra 或 arXiv 的 DOI（10.48550/arXiv.xxx）里。
"""

import re
from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from litgleaner.adapters.common.records import RecordBuilder
from litgleaner.adapters.common.text import parse_date, strip_markup
from litgleaner.domain.identity import normalize_arxiv_id, normalize_doi

NON_REGULAR_TYPES = frozenset({"attachment", "note", "annotation"})
VENUE_FIELDS = ("publicationTitle", "proceedingsTitle", "bookTitle", "repository")

_EXTRA_LINE = re.compile(r"^\s*([A-Za-z][A-Za-z .]*?)\s*:\s*(\S+)", re.MULTILINE)
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.", re.IGNORECASE)
# 本地库的 date 字段是“YYYY-MM-DD 原文”，月、日未知时为 00。
_MULTIPART_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:\s|$)")
_MONTH_YEAR = re.compile(r"^(\d{1,2})[/.](\d{4})$")
_YEAR = re.compile(r"\b(\d{4})\b")


def text(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, int | float) and not isinstance(value, bool):
        value = str(value)
    return (value.strip() or None) if isinstance(value, str) else None


def is_regular(data: Mapping[str, Any]) -> bool:
    """常规条目：不是附件、笔记、批注，也不在回收站。"""
    return data.get("itemType") not in NON_REGULAR_TYPES and not data.get("deleted")


def extra_values(data: Mapping[str, Any]) -> dict[str, str]:
    """extra 字段里 "Key: value" 形式的行，键小写；同名取第一行。"""
    values: dict[str, str] = {}
    for label, value in _EXTRA_LINE.findall(text(data, "extra") or ""):
        values.setdefault(label.lower(), value)
    return values


def item_doi(data: Mapping[str, Any]) -> str | None:
    return normalize_doi(text(data, "DOI")) or normalize_doi(extra_values(data).get("doi"))


def item_arxiv(data: Mapping[str, Any]) -> str | None:
    doi = text(data, "DOI") or ""
    candidates = (
        text(data, "archiveID"),
        extra_values(data).get("arxiv"),
        text(data, "url"),
        _ARXIV_DOI.sub("", doi) if _ARXIV_DOI.match(doi) else None,
    )
    return next((found for c in candidates if (found := normalize_arxiv_id(c))), None)


def zotero_date(value: str | None) -> date | None:
    """尽力解析 Zotero 的日期：2024-03-00 March 2024、2007-03、03/2024、2024/05/12、Spring 1999。"""
    if not value:
        return None
    raw = value.strip()
    multipart = _MULTIPART_DATE.match(raw)
    if multipart:
        year, month, day = multipart.groups()
        parts = [int(year)]
        if month != "00":
            parts += [int(month)] + ([int(day)] if day != "00" else [])
        return parse_date(parts)
    found = parse_date(raw.replace("/", "-"))
    if found:
        return found
    month_year = _MONTH_YEAR.match(raw)
    if month_year:
        return parse_date([int(month_year.group(2)), int(month_year.group(1))])
    year = _YEAR.search(raw)
    return parse_date([int(year.group(1))]) if year else None


def _name(creator: Mapping[str, Any]) -> str:
    if text(creator, "name"):
        return text(creator, "name") or ""
    parts = (text(creator, "firstName"), text(creator, "lastName"))
    return " ".join(p for p in parts if p)


def item_authors(creators: Any) -> tuple[str, ...]:
    """优先取作者；没有作者类型时（专利发明人、演讲者等）退回全部创作者。"""
    entries = [c for c in creators if isinstance(c, Mapping)] if isinstance(creators, list) else []
    authors = [c for c in entries if c.get("creatorType") == "author"] or entries
    return tuple(name for name in (_name(c) for c in authors) if name)


def _first_issn(value: str | None) -> str | None:
    tokens = re.split(r"[,;\s]+", value or "")
    return next((token for token in tokens if token), None)


def _first_text(data: Mapping[str, Any], keys: Iterable[str]) -> str | None:
    return next((value for key in keys if (value := text(data, key))), None)


def add_item(
    builder: RecordBuilder,
    data: Mapping[str, Any],
    zotero_id: str,
    parsed_date: str | None = None,
) -> None:
    extra = extra_values(data)
    builder.add(
        title=strip_markup(text(data, "title")) or "",
        abstract=strip_markup(text(data, "abstractNote")),
        authors=item_authors(data.get("creators")),
        venue=_first_text(data, VENUE_FIELDS),
        issn=_first_issn(text(data, "ISSN")),
        published_at=zotero_date(parsed_date or text(data, "date")),
        doi=item_doi(data),
        arxiv_id=item_arxiv(data),
        external_ids={
            "zotero": zotero_id,
            "pmid": text(data, "PMID") or extra.get("pmid"),
            "pmcid": text(data, "PMCID") or extra.get("pmcid"),
        },
    )
