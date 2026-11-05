"""统一文档格式。解析器的差异到此为止，下游只认这里。"""

from dataclasses import asdict, dataclass
from typing import Any, Literal

BlockKind = Literal["heading", "text", "table", "equation", "figure", "code", "reference"]
Quality = Literal["ok", "degraded"]
DOCUMENT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    text: str
    page: int
    bbox: tuple[int, int, int, int] | None = None
    caption: str | None = None


@dataclass(frozen=True)
class NormalizedDocument:
    schema_version: int
    source_pdf_sha256: str
    parser_id: str
    title: str | None
    blocks: tuple[Block, ...]
    front_matter: tuple[str, ...]
    quality: Quality

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "NormalizedDocument":
        blocks = tuple(
            Block(**{**block, "bbox": tuple(block["bbox"]) if block.get("bbox") else None})
            for block in data["blocks"]
        )
        return NormalizedDocument(
            schema_version=data["schema_version"],
            source_pdf_sha256=data["source_pdf_sha256"],
            parser_id=data["parser_id"],
            title=data["title"],
            blocks=blocks,
            front_matter=tuple(data["front_matter"]),
            quality=data["quality"],
        )
