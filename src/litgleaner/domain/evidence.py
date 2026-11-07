"""LLM 输入的筛选和隔离，以及证据校验。"""

import re
from collections.abc import Iterable

from litgleaner.domain.chunker import Chunk

MIN_QUOTE_CHARS = 10  # 太短的引文几乎总能匹配上，证明不了什么
_WHITESPACE = re.compile(r"\s+")
_TAG_CLOSE = re.compile(r"</\s*untrusted_paper\s*>", re.IGNORECASE)
_TAG_OPEN = re.compile(r"<\s*untrusted_paper\s*>", re.IGNORECASE)

# 按判断价值排序；预算不够时优先留前面的。other 不进入 LLM。
_PRIORITY = (
    "abstract",
    "conclusion",
    "limitation",
    "discussion",
    "results",
    "introduction",
    "method",
)


def _squash(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()


def verify_quote(quote: str | None, source: str) -> bool:
    """只能证明引文存在于解析文本，解析文本本身也可能有识别错误。"""
    needle = _squash(quote or "")
    return len(needle) >= MIN_QUOTE_CHARS and needle in _squash(source)


def select_chunks(chunks: Iterable[Chunk], token_budget: int) -> tuple[Chunk, ...]:
    """按章节优先级选取，放不下的跳过，结果恢复文档原始顺序。"""
    ordered = list(chunks)
    picked: set[str] = set()
    remaining = token_budget
    for section_type in _PRIORITY:
        for chunk in ordered:
            if chunk.section_type == section_type and chunk.token_count <= remaining:
                picked.add(chunk.chunk_id)
                remaining -= chunk.token_count
    return tuple(chunk for chunk in ordered if chunk.chunk_id in picked)


def wrap_untrusted(text: str) -> str:
    """论文内容只是数据。先清除分隔标记，防止内容提前闭合后伪装成指令。"""
    cleaned = _TAG_OPEN.sub("[removed]", _TAG_CLOSE.sub("[removed]", text))
    return f"<untrusted_paper>\n{cleaned}\n</untrusted_paper>"
