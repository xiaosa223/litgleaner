"""翻译的段落单位（第 12.3 节，纯函数）。

解析出的块会把一个段落按分页、分栏、图表切成几块。前一块末尾没有句末标点、后一块以小写开头时，
两块属于同一段，拼成一段再分句，这样跨块的句子仍是一句、一个编号。
译文按各块原文所占的长度切回各块，供按块叠加和段落视图显示。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise

from litgleaner.domain.document import Block
from litgleaner.domain.sentences import split_sentences

_SENTENCE_END = ".!?。！？"
_CLOSERS = "\"'”’)]）】"
_TRANSPARENT = frozenset({"figure", "table"})  # 图表夹在段落中间，不打断
_CUT_MARKS = "，。、；：！？,;: "
_CUT_WINDOW = 3


@dataclass(frozen=True)
class Fragment:
    index: int  # 块序号
    start: int  # 在段落文字中的位置
    end: int


@dataclass(frozen=True)
class Paragraph:
    fragments: tuple[Fragment, ...]
    text: str
    sentences: tuple[str, ...]
    spans: tuple[tuple[int, int], ...]  # 每句在段落文字中的位置

    @property
    def indices(self) -> tuple[int, ...]:
        return tuple(f.index for f in self.fragments)


@dataclass(frozen=True)
class BlockPart:
    sources: tuple[str, ...]  # 这一块里的原文（跨块的句子只算本块的部分）
    sentences: tuple[str, ...]  # 与 sources 一一对应的译文


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip().rstrip(_CLOSERS)
    return not stripped or stripped[-1] in _SENTENCE_END


def _continues(previous: str, following: str) -> bool:
    head = following.lstrip()[:1]
    return not _ends_sentence(previous) and head.isalpha() and head.islower()


def _chains(
    blocks: Sequence[Block], wanted: frozenset[int], max_parts: int, max_chars: int
) -> list[list[int]]:
    chains: list[list[int]] = []
    open_chain = False  # 上一个翻译块之后只隔着图表
    for index, block in enumerate(blocks):
        if index not in wanted:
            open_chain = open_chain and block.kind in _TRANSPARENT
            continue
        current = chains[-1] if chains else []
        joinable = (
            open_chain
            and len(current) < max_parts
            and sum(len(blocks[i].text) for i in current) + len(block.text) <= max_chars
            and _continues(blocks[current[-1]].text, block.text)
        )
        if joinable:
            current.append(index)
        else:
            chains.append([index])
        open_chain = True
    return chains


def _locate(text: str, sentences: Sequence[str]) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    cursor = 0
    for sentence in sentences:
        start = text.find(sentence, cursor)
        start = cursor if start < 0 else start  # 分句只做 strip，按理总能找到
        spans.append((start, start + len(sentence)))
        cursor = start + len(sentence)
    return tuple(spans)


def _paragraph(blocks: Sequence[Block], chain: Sequence[int]) -> Paragraph:
    fragments: list[Fragment] = []
    parts: list[str] = []
    cursor = 0
    for index in chain:
        piece = " ".join(blocks[index].text.split())  # 换行会破坏逐行编号协议
        fragments.append(Fragment(index, cursor, cursor + len(piece)))
        parts.append(piece)
        cursor += len(piece) + 1  # 块之间用一个空格连接
    text = " ".join(parts)
    sentences = tuple(split_sentences(text))
    return Paragraph(tuple(fragments), text, sentences, _locate(text, sentences))


def paragraphs(
    blocks: Sequence[Block],
    translatable: frozenset[int],
    *,
    max_parts: int = 4,
    max_chars: int = 4000,
) -> tuple[Paragraph, ...]:
    """按阅读顺序给出要翻译的段落。translatable 是需要翻译的块序号。"""
    chains = _chains(blocks, translatable, max_parts, max_chars)
    return tuple(_paragraph(blocks, chain) for chain in chains)


def _cut_points(translated: str, weights: Sequence[int]) -> list[int]:
    """按原文长度比例切译文，尽量切在标点或空格后面；每段至少一个字。"""
    total = sum(weights) or 1
    points: list[int] = []
    running = 0
    for weight in weights[:-1]:
        running += weight
        target = round(len(translated) * running / total)
        nearby = [
            p
            for p in range(target - _CUT_WINDOW, target + _CUT_WINDOW + 1)
            if 0 < p < len(translated) and translated[p - 1] in _CUT_MARKS
        ]
        point = min(nearby, key=lambda p: abs(p - target)) if nearby else target
        low = (points[-1] if points else 0) + 1
        points.append(max(low, min(point, len(translated) - 1)))
    return points


def _split(translated: str, weights: Sequence[int]) -> list[str]:
    if len(weights) == 1:
        return [translated]
    bounds = [0, *_cut_points(translated, weights), len(translated)]
    return [translated[a:b].strip() for a, b in pairwise(bounds)]


def slice_to_blocks(paragraph: Paragraph, translations: Sequence[str]) -> Mapping[int, BlockPart]:
    """把逐句译文分到各块。跨块的句子按各块原文长度切开，拼起来仍是整句译文。"""
    sources: dict[int, list[str]] = {f.index: [] for f in paragraph.fragments}
    targets: dict[int, list[str]] = {f.index: [] for f in paragraph.fragments}
    for (start, end), translated in zip(paragraph.spans, translations, strict=True):
        pieces = [
            (f.index, paragraph.text[max(start, f.start) : min(end, f.end)].strip())
            for f in paragraph.fragments
            if max(start, f.start) < min(end, f.end)
        ]
        pieces = [(index, piece) for index, piece in pieces if piece]
        cut = _split(translated, [len(piece) for _, piece in pieces])
        for (index, piece), part in zip(pieces, cut, strict=True):
            sources[index].append(piece)
            targets[index].append(part)
    return {index: BlockPart(tuple(sources[index]), tuple(targets[index])) for index in sources}
