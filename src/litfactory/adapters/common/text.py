"""外部文本清洗：去掉 JATS/HTML 标记，解析不完整的日期。"""

import html
import re
from collections.abc import Sequence
from datetime import date

# 只匹配真正的标签形状（<p>、</jats:italic>、<sub class="x">），不吞掉正文里的 “< 0.1”。
_TAG = re.compile(r"</?[A-Za-z][\w:.-]*(?:\s[^<>]*)?/?>")
_SPACE = re.compile(r"\s+")
_DATE = re.compile(r"^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?")


def strip_markup(value: object) -> str | None:
    """先反转义再去标签：转义过的标签（&lt;i&gt;）也能去掉，正文里的比较符号保留。
    外部数据类型不对时返回 None，不抛异常。"""
    if not isinstance(value, str) or not value:
        return None
    text = _SPACE.sub(" ", _TAG.sub(" ", html.unescape(value))).strip()
    return text or None


def _leading_ints(values: Sequence[object]) -> list[int]:
    """Crossref 等来源的 date-parts 里会出现 null 或字符串，只取开头连续的整数部分。"""
    parts: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            break
        parts.append(value)
    return parts


def parse_date(value: object) -> date | None:
    """接受 2026-11-05、2026-11、2026 和 [2026, 11, 5]。只有年份时取当年 1 月 1 日。"""
    if value is None:
        return None
    if isinstance(value, str):
        match = _DATE.match(value.strip())
        if not match:
            return None
        parts = [int(p) for p in match.groups() if p]
    elif isinstance(value, list | tuple):
        parts = _leading_ints(value)
    else:
        return None
    if not parts:
        return None
    year, month, day = (*parts, 1, 1)[:3]
    try:
        return date(year, month, day)
    except ValueError:
        return None
