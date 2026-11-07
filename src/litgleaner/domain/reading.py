"""阅读进度和批注。批注记录页码、bbox 和原文片段，不依赖 chunk_id，切分规则变了也能定位。"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

ReadingStatus = Literal["queued", "reading", "read", "abandoned"]
AnnotationKind = Literal["highlight", "note"]
READING_STATUSES: frozenset[str] = frozenset(get_args(ReadingStatus))
# 坐标都是页面归一化坐标 0–1000，原点在左上角（与统一文档的 bbox 一致）
BBox = tuple[int, int, int, int]  # x0, y0, x1, y1
Rect = tuple[int, int, int, int]  # x, y, 宽, 高；一段高亮跨多行时有多个


@dataclass(frozen=True)
class ReadingState:
    paper_id: str
    status: ReadingStatus
    position: Mapping[str, Any] | None  # 读到的位置，例如 {"view": "pdf", "page": 3}
    updated_at: str


@dataclass(frozen=True)
class NewAnnotation:
    paper_id: str
    kind: AnnotationKind
    page: int | None
    bbox: BBox | None
    quote: str | None  # 原文片段
    body: str | None  # 用户写的笔记
    rects: tuple[Rect, ...] = ()  # 原版视图里高亮的位置


@dataclass(frozen=True)
class Annotation:
    id: str
    paper_id: str
    kind: AnnotationKind
    page: int | None
    bbox: BBox | None
    quote: str | None
    body: str | None
    created_at: str
    updated_at: str
    rects: tuple[Rect, ...] = ()
