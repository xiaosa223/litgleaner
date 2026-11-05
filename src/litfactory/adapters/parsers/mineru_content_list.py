"""把 MinerU 的 content_list.json 转成统一文档格式。

字段来自 2026-11-02 对一篇 IEEE 风格论文的实测，官方文档未给出定义，
所以未知类型保留文本并降级，结构不符直接报错，不静默丢内容。
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from litfactory.domain.document import (
    DOCUMENT_SCHEMA_VERSION,
    Block,
    BlockKind,
    NormalizedDocument,
)
from litfactory.domain.errors import SchemaChanged

_DROPPED = frozenset({"page_number", "aside_text"})
_BBOX_LENGTH = 4


def _join(value: Any) -> str | None:
    if isinstance(value, list | tuple):
        return " ".join(str(part) for part in value if part) or None
    return str(value) if value else None


def _bbox(item: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    value = item.get("bbox")
    if isinstance(value, list) and len(value) == _BBOX_LENGTH:
        return (int(value[0]), int(value[1]), int(value[2]), int(value[3]))
    return None


def _text_block(item: Mapping[str, Any]) -> Block:
    kind: BlockKind = "heading" if item.get("text_level") else "text"
    return Block(kind, str(item.get("text", "")), item["page_idx"], _bbox(item))


def _simple(kind: BlockKind) -> Callable[[Mapping[str, Any]], Block]:
    def build(item: Mapping[str, Any]) -> Block:
        return Block(kind, str(item.get("text", "")), item["page_idx"], _bbox(item))

    return build


def _table(item: Mapping[str, Any]) -> Block:
    caption = _join(item.get("table_caption"))
    return Block("table", str(item.get("table_body", "")), item["page_idx"], _bbox(item), caption)


def _chart(item: Mapping[str, Any]) -> Block:
    caption = _join(item.get("chart_caption"))
    return Block("figure", str(item.get("content") or ""), item["page_idx"], _bbox(item), caption)


def _code(item: Mapping[str, Any]) -> Block:
    caption = _join(item.get("code_caption"))
    return Block("code", str(item.get("code_body", "")), item["page_idx"], _bbox(item), caption)


_BUILDERS: Mapping[str, Callable[[Mapping[str, Any]], Block]] = {
    "text": _text_block,
    "equation": _simple("equation"),
    "table": _table,
    "chart": _chart,
    "code": _code,
    "ref_text": _simple("reference"),
}


def _check_item(item: Any) -> Mapping[str, Any]:
    if not isinstance(item, Mapping) or "type" not in item or "page_idx" not in item:
        raise SchemaChanged(f"MinerU content_list 条目结构不符：{str(item)[:120]}")
    return item


def _is_title(item: Mapping[str, Any], title: str | None) -> bool:
    return (
        title is None
        and item["type"] == "text"
        and item.get("text_level") == 1
        and item["page_idx"] == 0
    )


@dataclass(frozen=True)
class _Parsed:
    title: str | None
    blocks: tuple[Block, ...]
    front_matter: tuple[str, ...]
    degraded: bool


def _parse_items(items: list[Any]) -> _Parsed:
    title: str | None = None
    blocks: list[Block] = []
    front_matter: list[str] = []
    degraded = False
    for raw in items:
        item = _check_item(raw)
        kind = item["type"]
        if _is_title(item, title):
            title = str(item.get("text", "")).strip() or None
        elif kind == "page_footnote":
            front_matter.append(str(item.get("text", "")))
        elif kind in _BUILDERS:
            blocks.append(_BUILDERS[kind](item))
        elif kind not in _DROPPED:
            degraded = True
            blocks.append(_simple("text")(item))
    return _Parsed(title, tuple(blocks), tuple(front_matter), degraded)


def to_normalized_document(items: Any, pdf_sha256: str, parser_id: str) -> NormalizedDocument:
    if not isinstance(items, list):
        raise SchemaChanged("MinerU content_list 应为数组")
    parsed = _parse_items(items)
    return NormalizedDocument(
        schema_version=DOCUMENT_SCHEMA_VERSION,
        source_pdf_sha256=pdf_sha256,
        parser_id=parser_id,
        title=parsed.title,
        blocks=parsed.blocks,
        front_matter=parsed.front_matter,
        quality="degraded" if parsed.degraded else "ok",
    )
