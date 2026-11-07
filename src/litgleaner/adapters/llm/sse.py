"""Server-Sent Events 解析：把逐行文本组装成 (事件名, data) 对。"""

from collections.abc import Iterable, Iterator


def iter_sse(lines: Iterable[str]) -> Iterator[tuple[str | None, str]]:
    """空行结束一个事件；多行 data 用换行连接；注释行（以冒号开头）忽略。"""
    name: str | None = None
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if not line:
            if data:
                yield name, "\n".join(data)
            name, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield name, "\n".join(data)
