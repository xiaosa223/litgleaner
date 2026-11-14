"""用户对论文元数据的修正（ADR 0011）：只在用户看到的地方覆盖来源元数据，事实层不改。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from typing import Literal

from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.paper import Paper

CorrectionField = Literal["title", "authors", "venue", "published_at"]
FIELDS: tuple[CorrectionField, ...] = ("title", "authors", "venue", "published_at")
LIMITS: Mapping[str, int] = {"title": 500, "authors": 2000, "venue": 300, "published_at": 20}
_STRONG = re.compile(r"\s*[、;；\n]\s*")  # 有这些分隔符时，逗号属于“姓, 名”
_COMMA = re.compile(r"\s*[,，]\s*")
_SECTION = re.compile(
    r"^(\d+\.?\s*)?(abstract|introduction|摘\s*要|引\s*言|keywords?|index terms)\b", re.IGNORECASE
)
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$", re.ASCII)


@dataclass(frozen=True)
class Correction:
    id: str
    paper_id: str
    field: CorrectionField
    value: str  # 空串表示撤销这个字段的修正
    created_at: str


def _valid_date(value: str) -> bool:
    if not _DATE.match(value):
        return False
    parts = [int(part) for part in value.split("-")]
    try:
        date(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
    except ValueError:
        return False
    return True


def validate(field: str, value: str) -> str:
    if field not in FIELDS:
        raise ValueError(f"不能修正的字段：{field}")
    value = " ".join(value.split())
    if len(value) > LIMITS[field]:
        raise ValueError(f"{field} 太长")
    if field == "published_at" and value and not _valid_date(value):
        raise ValueError("年份写成 2016 或 2016-08-09")
    return value


def split_authors(value: str) -> tuple[str, ...]:
    """顿号、分号、换行分隔；都没有时才按逗号分。“Smith, John; Lee, Amy”是两位作者。"""
    pattern = _STRONG if _STRONG.search(value) else _COMMA
    return tuple(name.strip() for name in pattern.split(value) if name.strip())


def apply(paper: Paper, latest: Mapping[str, str]) -> Paper:
    """把每个字段最新的一条修正覆盖到显示用的论文上；空值表示沿用来源元数据。"""
    changes: dict[str, object] = {}
    for field, value in latest.items():
        if not value:
            continue
        if field == "authors":
            changes["authors"] = split_authors(value)
        else:
            changes[field] = value
    return replace(paper, **changes) if changes else paper  # type: ignore[arg-type]


def title_candidate(document: NormalizedDocument) -> str | None:
    """从解析结果猜标题：解析器给出的标题，其次首页第一个标题块，再其次首页第一段长度合适的文字。"""
    if document.title and 8 <= len(document.title.strip()) <= 300:
        return " ".join(document.title.split())
    first = [block for block in document.blocks if block.page == 0 and block.text.strip()]
    for block in first[:6]:
        if _SECTION.match(block.text.strip()):
            continue  # ABSTRACT、1. INTRODUCTION 这类章节名不是标题
        if block.kind == "heading" and 8 <= len(block.text.strip()) <= 300:
            return " ".join(block.text.split())
    for block in first[:6]:
        text = " ".join(block.text.split())
        if block.kind == "text" and 8 <= len(text) <= 250 and "@" not in text:
            return text
    return None
