"""按句编号分批翻译（第 12.3 节）。

每批约一页正文，每句一行、以 [[n]] 开头；模型逐行输出 [[n]] 译文。
一个段落的句子总在同一批里，这样段落一完成就能校验并显示，不等整批。
"""

import re
from collections.abc import Sequence

BATCH_CHARS = 4500  # 约一页双栏正文
_MARKER = re.compile(r"^\s*\[\[\s*(\d+)\s*\]\]\s*(.*)$")


def group_batches(sizes: Sequence[int], limit: int = BATCH_CHARS) -> tuple[tuple[int, ...], ...]:
    """按顺序把段落装进批次，每批字数不超过 limit；单段超过 limit 时自成一批。"""
    batches: list[list[int]] = []
    used = 0
    for index, size in enumerate(sizes):
        if batches and used + size <= limit:
            batches[-1].append(index)
            used += size
        else:
            batches.append([index])
            used = size
    return tuple(tuple(batch) for batch in batches)


def numbered_payload(sentences: Sequence[str]) -> str:
    return "\n".join(f"[[{n}]] {s}" for n, s in enumerate(sentences, start=1))


class LineParser:
    """逐段喂入流式输出，返回已经完整的编号行。只在一次调用内使用。

    没有编号的非空行记在 stray 里：开头的客套话直接忽略；整段输出都没有编号时，
    调用方可以按行的位置对齐（单段重试时的回退）。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._stray: list[str] = []

    @property
    def stray(self) -> tuple[str, ...]:
        return tuple(self._stray)

    def feed(self, chunk: str) -> list[tuple[int, str]]:
        self._buffer += chunk
        *complete, self._buffer = self._buffer.split("\n")
        return self._parse(complete)

    def finish(self) -> list[tuple[int, str]]:
        rest, self._buffer = self._buffer, ""
        return self._parse([rest])

    def _parse(self, lines: Sequence[str]) -> list[tuple[int, str]]:
        found: list[tuple[int, str]] = []
        for line in lines:
            match = _MARKER.match(line)
            if match:
                found.append((int(match.group(1)), match.group(2).strip()))
            elif line.strip():
                self._stray.append(line.strip())
        return found
