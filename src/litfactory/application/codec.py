"""文本和 JSON 的压缩存储。"""

import json
from collections.abc import Mapping
from typing import Any

import zstandard


def compress(content: bytes) -> bytes:
    return zstandard.ZstdCompressor(level=10).compress(content)


def decompress(content: bytes) -> bytes:
    return zstandard.ZstdDecompressor().stream_reader(content).read()


def compress_json(value: Mapping[str, Any]) -> bytes:
    return compress(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def decompress_json(content: bytes) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(decompress(content))
    return loaded
