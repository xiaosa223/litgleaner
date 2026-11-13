"""从 PDF 文字层估算每行、每段在页面上的位置，供原版视图放按钮和覆盖译文。

左、上、下边来自文字矩阵，是准确的；右边按字体的字宽表累加，查不到时按平均字宽估计。
只用来定位，不影响文字和切分。旋转页、竖排文字不给坐标，宁缺勿错。
"""

import math
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from pypdf import PageObject

_ASCENT = 0.8  # 基线以上占字号的比例
_DESCENT = 0.2
_NARROW = 0.5  # 没有字宽表时，西文字宽约半个字号，中日韩全角字约一个字号
_UPRIGHT = 0.1  # 矩阵里旋转分量超过这个比例就当作斜排或竖排
_SCALE = 1000
_MARGIN = 50  # 准确的边越出页面这么多，就当作页面外的文字，宁可不给坐标

# 片段坐标是 PDF 单位（y 向上：x0, 下边, x1, 上边）；行坐标换成千分比（y 向下）
Box = tuple[float, float, float, float]
# 单个字符的宽度，单位是字号
Widths = Callable[[str], float]


def _guess(char: str) -> float:
    return 1.0 if unicodedata.east_asian_width(char) in "WF" else _NARROW


def _font_widths(font: Any) -> Widths:
    """简单字体（Type1、TrueType）带 /Widths 表；复合字体、表损坏时退回估计值。"""
    try:
        table = [float(w) / 1000 for w in font["/Widths"]]
        first = int(font.get("/FirstChar", 0))
    except Exception:  # 字体字典来自不可信的 PDF，结构不对就不用
        return _guess
    known = [w for w in table if w > 0]
    if not known:
        return _guess
    average = sum(known) / len(known)

    def width(char: str) -> float:
        # 回调给的是解码后的字符，不是字体编码；自定义编码、连字时查到的宽度只是近似，只影响右边
        code = ord(char) - first
        return table[code] if 0 <= code < len(table) and table[code] > 0 else average

    return width


# 片段起点：x, 下边, 上边, 每单位字宽对应的横向长度
Origin = tuple[float, float, float, float]


@dataclass(frozen=True)
class _Fragment:
    text: str
    origin: Origin | None
    widths: Widths


def _origin(cm: Sequence[float], tm: Sequence[float], size: float) -> Origin | None:
    a = tm[0] * cm[0] + tm[1] * cm[2]
    b = tm[0] * cm[1] + tm[1] * cm[3]
    c = tm[2] * cm[0] + tm[3] * cm[2]
    d = tm[2] * cm[1] + tm[3] * cm[3]
    if size <= 0 or a <= 0 or d <= 0 or abs(b) > _UPRIGHT * a or abs(c) > _UPRIGHT * d:
        return None
    x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
    y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
    height = size * d
    origin = (x, y - _DESCENT * height, y + _ASCENT * height, size * a)
    return origin if all(math.isfinite(v) for v in origin) else None


def _union(boxes: Sequence[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _char_boxes(fragments: Sequence[_Fragment]) -> list[Box | None]:
    """从片段起点按字宽逐字累加出每个字符的位置，换行和空白没有位置。"""
    chars: list[Box | None] = []
    for fragment in fragments:
        if fragment.origin is None:
            chars.extend([None] * len(fragment.text))
            continue
        x, bottom, top, unit = fragment.origin
        for char in fragment.text:
            end = x + fragment.widths(char) * unit
            chars.append(None if char.isspace() else (x, bottom, end, top))
            x = end
    return chars


def _normalize(box: Box, page: PageObject) -> Box | None:
    """PDF 坐标换成千分比、原点在左上（ARCHITECTURE 9.1）：x0, 上边, x1, 下边。"""
    crop = page.cropbox
    left, top = float(crop.left), float(crop.top)
    width, height = float(crop.width), float(crop.height)
    if width <= 0 or height <= 0:
        return None
    x0, y0, x1, y1 = box
    scaled = (
        (x0 - left) / width * _SCALE,
        (top - y1) / height * _SCALE,
        (x1 - left) / width * _SCALE,
        (top - y0) / height * _SCALE,
    )
    # 左、上、下边来自文字矩阵，越出页面说明是页面外的文字；右边是估计值，超出就夹到页边
    exact = (scaled[0], scaled[1], scaled[3])
    inside = all(math.isfinite(v) and -_MARGIN <= v <= _SCALE + _MARGIN for v in exact)
    return (*scaled[:2], min(scaled[2], _SCALE), scaled[3]) if inside else None


def page_lines(page: PageObject) -> list[tuple[str, Box | None]]:
    """按 extract_text 的原样分行；每行带千分比位置，算不出时为 None。"""
    fragments: list[_Fragment] = []
    fonts: dict[int, Widths] = {}

    def visit(text: str, cm: Any, tm: Any, font: Any, size: Any) -> None:
        widths = fonts.get(id(font)) if font is not None else _guess
        if widths is None:
            widths = fonts[id(font)] = _font_widths(font)
        try:
            origin = _origin(cm, tm, float(size or 0))
        except (TypeError, ValueError, IndexError):  # 矩阵或字号不像样：这一段不给坐标
            origin = None
        fragments.append(_Fragment(text, origin, widths))

    text = page.extract_text(visitor_text=visit) or ""
    chars = _char_boxes(fragments)
    # 回调拼起来和输出逐字相同才能对位；pypdf 行为变化时放弃坐标，文字照常
    usable = page.rotation % 360 == 0 and "".join(f.text for f in fragments) == text
    lines: list[tuple[str, Box | None]] = []
    offset = 0
    for raw in text.splitlines(keepends=True):
        boxes = [b for b in chars[offset : offset + len(raw)] if b] if usable else []
        offset += len(raw)
        if raw.strip():
            lines.append((raw.strip(), _normalize(_union(boxes), page) if boxes else None))
    return lines


def _main_column(boxes: Sequence[Box]) -> list[Box]:
    """段落跨栏续写时按“下一行整个跑到上一行上方”切成几段，取行数最多的一段。"""
    runs: list[list[Box]] = [[boxes[0]]]
    for previous, box in pairwise(boxes):
        if box[3] < previous[1]:
            runs.append([box])
        else:
            runs[-1].append(box)
    return max(runs, key=len)


def block_bbox(boxes: Sequence[Box | None]) -> tuple[int, int, int, int] | None:
    """若干行的千分比位置合成块坐标，取整并限制在页面内。"""
    known = [b for b in boxes if b is not None]
    if not known:
        return None
    x0, y0, x1, y1 = (min(_SCALE, max(0, round(v))) for v in _union(_main_column(known)))
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None
