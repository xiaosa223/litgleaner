"""提取事实只核验真实页与实际发送正文，链接和坐标另作精确核对。"""

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, TypeGuard
from urllib.parse import urlsplit

from litgleaner.application.knowledge_ingest import ExtractionSource, SentBlock, sent_page_texts
from litgleaner.domain.document import Block, NormalizedDocument
from litgleaner.domain.evidence import MIN_QUOTE_CHARS, clean_untrusted, verify_quote
from litgleaner.domain.reading import BBox

_SINGLE = ("problem", "prior_gap", "approach", "method")
_LIST = ("claims", "datasets", "metrics", "results")
_ISOLATION = re.compile(r"\[removed\]|<\s*/?\s*untrusted_paper\s*>", re.IGNORECASE)
_URL_TOKEN = re.compile(r"(?<![A-Za-z0-9_])https?://[^\s<>\"`]+", re.IGNORECASE)
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_ENCODED_CONTROL = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", re.IGNORECASE)


def _body(block: Block) -> str:
    return "\n".join(part for part in (block.caption, block.text) if part)


@dataclass(frozen=True)
class _Pages:
    original: Mapping[int, str]
    sent: Mapping[int, tuple[str, ...]]
    blocks: tuple[SentBlock, ...]
    actual: str


def _pages(document: NormalizedDocument, source: ExtractionSource) -> _Pages:
    original: dict[int, list[str]] = defaultdict(list)
    actual = frozenset(document.blocks)
    sent = sent_page_texts(source)
    valid = all(block.block in actual for block in source.sources)
    for block in document.blocks:
        original[block.page].append(_body(block))
    return _Pages(
        {page: "\n".join(parts) for page, parts in original.items()},
        sent if valid else {},
        source.sources if valid else (),
        clean_untrusted(source.text),
    )


def _verified(item: Mapping[str, Any], pages: _Pages) -> bool:
    quote, page = item.get("quote"), item.get("page")
    return (
        isinstance(quote, str)
        and len(" ".join(quote.split())) >= MIN_QUOTE_CHARS
        and _ISOLATION.search(quote) is None
        and type(page) is int
        and page >= 0
        and verify_quote(quote, pages.original.get(page, ""))
        and any(verify_quote(quote, run) for run in pages.sent.get(page, ()))
        and verify_quote(quote, pages.actual)
    )


def _evidence(item: Mapping[str, Any], pages: _Pages) -> dict[str, Any]:
    return {**item, "verified": _verified(item, pages)}


def _highlight(item: Mapping[str, Any], pages: _Pages) -> dict[str, Any]:
    verified = _verified(item, pages)
    bbox: BBox | None = None
    if verified:
        bbox = next(
            (
                sent.block.bbox
                for sent in pages.blocks
                if sent.page == item["page"]
                and sent.block.bbox is not None
                and verify_quote(item["quote"], _body(sent.block))
                and verify_quote(item["quote"], clean_untrusted(sent.text))
            ),
            None,
        )
    return {**item, "verified": verified, "bbox": bbox}


def _valid_host(host: str) -> bool:
    if ":" in host or re.fullmatch(r"[0-9.]+", host):
        ip_address(host)
        return True
    encoded = host.encode("idna").decode("ascii").removesuffix(".")
    return len(encoded) <= 253 and all(_HOST_LABEL.fullmatch(part) for part in encoded.split("."))


def _valid_url(url: Any) -> TypeGuard[str]:
    if not isinstance(url, str) or _URL_TOKEN.fullmatch(url) is None:
        return False
    if (
        any(not char.isprintable() or char.isspace() for char in url)
        or "\\" in url
        or _ENCODED_CONTROL.search(url)
    ):
        return False
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme not in ("http", "https") or not host or parsed.username is not None:
            return False
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            return False
        return _valid_host(host)
    except (ValueError, UnicodeError):
        return False


def _url_token(token: str) -> str:
    """去掉正文终止标点和外层闭括号，保留 URL 自身成对的括号。"""
    pairs = {")": "(", "]": "[", "}": "{"}
    while token:
        trimmed = token.rstrip(".,;:!?，。；：！？、")
        if trimmed != token:
            token = trimmed
        elif token[-1] in pairs and token.count(token[-1]) > token.count(pairs[token[-1]]):
            token = token[:-1]
        else:
            break
    return token


def _delimited_url(match: re.Match[str], text: str) -> str | None:
    """明确包围的 URL 有自己的边界，末尾标点也属于 URL。"""
    before, token = text[: match.start()], match.group()
    if before[-1:] == "'":
        return token[: token.rfind("'")] if "'" in token else token
    if before[-1:] in ("<", '"', "`"):
        return token
    markdown = re.search(r"\]\(\s*$", before) is not None
    pairs = {"(": ")", "[": "]", "{": "}"}
    opening = "(" if markdown else before[-1:]
    if opening not in pairs:
        return None
    closing, depth = pairs[opening], 0
    for position, char in enumerate(token):
        if char == opening:
            depth += 1
        elif char == closing:
            if depth == 0:
                return token[:position]
            depth -= 1
    return token if markdown else None


def _has_url(url: str, text: str) -> bool:
    for match in _URL_TOKEN.finditer(text):
        explicit = _delimited_url(match, text)
        token = (
            explicit
            if explicit is not None
            else _url_token(re.split(r"[，。；：！？、]", match.group(), maxsplit=1)[0])
        )
        if token == url:
            return True
    return False


def _code_data(item: Mapping[str, Any], pages: _Pages) -> dict[str, Any]:
    checked = _evidence(item, pages)
    url = item.get("url")
    approved = (
        checked["verified"]
        and _valid_url(url)
        and _has_url(url, item["quote"])
        and _has_url(url, pages.original[item["page"]])
        and any(_has_url(url, run) for run in pages.sent[item["page"]])
    )
    return {**checked, "url": url if approved else None, "url_verified": bool(approved)}


def verify_extraction(
    data: Mapping[str, Any], document: NormalizedDocument, source: ExtractionSource
) -> dict[str, Any]:
    """接收已通过结构校验的结果；保留旧字段，不补造缺失内容或模型答案。"""
    pages = _pages(document, source)
    checked = dict(data)
    for name in _SINGLE:
        if name in data:
            checked[name] = _evidence(data[name], pages) if data[name] is not None else None
    for name in _LIST:
        if name in data:
            checked[name] = [_evidence(item, pages) for item in data[name]]
    if "limitations" in data:
        checked["limitations"] = [
            {**item, "verified": item["source"] == "author" and _verified(item, pages)}
            for item in data["limitations"]
        ]
    if "code_data" in data:
        checked["code_data"] = [_code_data(item, pages) for item in data["code_data"]]
    if "questions" in data:
        checked["questions"] = [
            {
                **item,
                "answer": _evidence(item["answer"], pages) if item["answer"] is not None else None,
            }
            for item in data["questions"]
        ]
    if "highlights" in data:
        checked["highlights"] = [_highlight(item, pages) for item in data["highlights"]]
    return checked
