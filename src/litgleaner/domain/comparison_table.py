"""对比表只渲染已保存的提取与评估，不补造缺失事实或核验结论。"""

import csv
import html
import io
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Literal
from urllib.parse import urlsplit

from litgleaner.domain.goal_links import GoalLink

_HEADERS = (
    "论文标题",
    "论文编号",
    "方法",
    "数据集",
    "指标与结果",
    "代码/数据公开情况",
    "阅读价值分",
    "对应目标",
    "状态",
)
_MISSING = "未提取"
_NO_EVIDENCE = "本次提取未获得可核验证据"
_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_ENCODED_CONTROL = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|7f)", re.IGNORECASE)


@dataclass(frozen=True)
class ComparisonRow:
    paper_id: str
    title: str
    extraction: Mapping[str, Any] | None
    extraction_stale: bool
    reading_value: int | None
    tier: str | None
    score_stale: bool
    provisional: bool | None
    goal_links: tuple[GoalLink, ...]
    goals: Mapping[str, str]


def _page(value: Any) -> str:
    return f"PDF 第 {value + 1} 页" if type(value) is int and value >= 0 else "页码未记录"


def _verification(item: Mapping[str, Any]) -> str:
    return "已核验" if item.get("verified") is True else "未核验"


def _raw(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _evidence(item: Any) -> str:
    if not isinstance(item, Mapping):
        return "原始记录（未核验）：" + _raw(item)
    text = item.get("text")
    quote = item.get("quote")
    parts = ["模型概括：" + (text if isinstance(text, str) else "未记录")]
    if isinstance(quote, str) and quote:
        parts.append(f"原文引用（{_verification(item)}）；{_page(item.get('page'))}：{quote}")
    else:
        parts.append("原文引用：未记录（未核验）")
    return "\n".join(parts)


def _field(data: Mapping[str, Any] | None, name: str) -> str:
    if data is None or name not in data:
        return _MISSING
    value = data[name]
    if value is None or value in ([], ()):
        return _NO_EVIDENCE
    if isinstance(value, list | tuple):
        return "\n\n".join(_evidence(item) for item in value)
    return _evidence(value)


def _valid_host(host: str) -> bool:
    if ":" in host or re.fullmatch(r"[0-9.]+", host):
        ip_address(host)
        return True
    encoded = host.encode("idna").decode("ascii").removesuffix(".")
    return len(encoded) <= 253 and all(_HOST_LABEL.fullmatch(part) for part in encoded.split("."))


def _valid_url(value: Any) -> bool:
    if not isinstance(value, str) or len(value) > 2048:
        return False
    if (
        not value
        or any(not char.isprintable() or char.isspace() for char in value)
        or any(char in value for char in '<>"`\\')
        or _ENCODED_CONTROL.search(value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
        ):
            return False
        if parsed.port is not None and not 0 < parsed.port <= 65535:
            return False
        return _valid_host(parsed.hostname)
    except (ValueError, UnicodeError):
        return False


def _code_item(item: Any) -> str:
    if not isinstance(item, Mapping):
        return "原始记录（未核验）：" + _raw(item)
    kind = {"code": "代码", "data": "数据"}.get(str(item.get("kind")), "代码/数据类型未知")
    availability = {"public": "公开", "not_public": "不公开", "unknown": "未知"}.get(
        str(item.get("availability")), "未知"
    )
    parts = [f"模型记录：{kind}公开状态：{availability}", _evidence(item)]
    url = item.get("url")
    parts.append(
        "原文链接（已核验）：" + url
        if item.get("url_verified") is True and _valid_url(url) and isinstance(url, str)
        else "链接未核验或未提供"
    )
    return "\n".join(parts)


def _code_data(data: Mapping[str, Any] | None) -> str:
    if data is None or "code_data" not in data:
        return _MISSING
    items = data["code_data"]
    if items is None or items in ([], ()):
        return _NO_EVIDENCE
    if not isinstance(items, list | tuple):
        return "原始记录（未核验）：" + _raw(items)
    return "\n\n".join(_code_item(item) for item in items)


def _score(row: ComparisonRow) -> str:
    if row.reading_value is None:
        return "未评分（历史评分已过期）" if row.score_stale else "未评分"
    parts = [str(row.reading_value)]
    if row.tier:
        parts.append(row.tier)
    if row.provisional is True:
        parts.append("暂定分")
    if row.score_stale:
        parts.append("已过期，不参与筛选")
    return "；".join(parts)


def _goals(row: ComparisonRow) -> str:
    parts: list[str] = []
    for link in row.goal_links:
        goal = row.goals.get(link.goal_id)
        target = f"{link.goal_id}：{goal}" if goal is not None else f"未知目标 {link.goal_id}"
        lines = [f"评估关联：{target}", f"模型推荐理由：{link.reuse}"]
        if link.quote:
            status = "已核验" if link.verified is True else "未核验"
            lines.append(f"原文引用（{status}）；{_page(link.page)}：{link.quote}")
        else:
            lines.append("原文引用：未记录（未核验）")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else "未记录目标关联"


def _status(row: ComparisonRow) -> str:
    parts = []
    if row.extraction_stale:
        parts.append("提取已过期")
    elif row.extraction is None:
        parts.append("无提取记录")
    if row.score_stale:
        parts.append("评分已过期")
    return "；".join(parts) if parts else "当前记录"


def _cells(row: ComparisonRow) -> tuple[str, ...]:
    return (
        row.title,
        row.paper_id,
        _field(row.extraction, "method"),
        _field(row.extraction, "datasets"),
        "指标："
        + _field(row.extraction, "metrics")
        + "\n\n结果："
        + _field(row.extraction, "results"),
        _code_data(row.extraction),
        _score(row),
        _goals(row),
        _status(row),
    )


def _markdown(value: str) -> str:
    text = html.escape(value, quote=False)
    text = re.sub(r"([\\`*_{}\[\]()#!~])", r"\\\1", text)
    text = text.replace("|", "&#124;").replace(":", "&#58;").replace("@", "&#64;")
    text = re.sub(r"\bwww\.", "www&#46;", text, flags=re.IGNORECASE)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def _spreadsheet(value: str) -> str:
    leading = ""
    for char in value:
        if not char.isspace() and not unicodedata.category(char).startswith("C"):
            break
        leading += char
    candidate = value[len(leading) :]
    unsafe = candidate.startswith(("=", "+", "@", "-")) or "\t" in leading or "\r" in leading
    return "'" + value if unsafe else value


def render_table(rows: tuple[ComparisonRow, ...], format: Literal["md", "csv"]) -> str:
    if format == "md":
        lines = [
            "| " + " | ".join(_HEADERS) + " |",
            "| " + " | ".join(["---"] * len(_HEADERS)) + " |",
        ]
        lines.extend(
            "| " + " | ".join(_markdown(cell) for cell in _cells(row)) + " |" for row in rows
        )
        return "\n".join(lines) + "\n"
    if format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\r\n")
        writer.writerow(_HEADERS)
        writer.writerows(tuple(_spreadsheet(cell) for cell in _cells(row)) for row in rows)
        return stream.getvalue()
    raise ValueError("comparison_table_format_invalid")
