"""Markdown 中保留原始内容和字段来源，不执行导入文本里的 HTML。"""

import html
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any


def label(value: str) -> str:
    text = html.escape(" ".join(value.splitlines()), quote=False)
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", text)


def literal(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    language = "text" if isinstance(value, str) else "json"
    return f"{fence}{language}\n{text}\n{fence}"


def page_label(page: Any) -> str:
    return f"PDF 第 {page + 1} 页" if type(page) is int and page >= 0 else "页码未记录"


def stored_quotes(value: Any) -> Iterator[str]:
    if isinstance(value, Mapping):
        quote = value.get("quote")
        if isinstance(quote, str) and quote:
            source = (
                "作者原文（已核验）" if value.get("verified") is True else "模型记录中的待核验引文"
            )
            yield f"{source}；{page_label(value.get('page'))}\n\n{literal(quote)}"
        for item in value.values():
            yield from stored_quotes(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from stored_quotes(item)
