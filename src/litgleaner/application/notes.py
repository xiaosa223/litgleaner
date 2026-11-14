"""知识库的“笔记”：汇总全部论文的笔记和高亮，只读，不调用模型（第 12.8 节）。"""

from dataclasses import dataclass

from litgleaner.application import corrections
from litgleaner.application.context import AppContext
from litgleaner.domain.errors import InvalidRequest
from litgleaner.domain.reading import Annotation


@dataclass(frozen=True)
class NoteView:
    annotation: Annotation
    paper_title: str


def recent(ctx: AppContext, limit: int = 100, offset: int = 0) -> tuple[NoteView, ...]:
    if not 1 <= limit <= 200 or offset < 0:
        raise InvalidRequest("笔记查询参数越界")
    items = ctx.store.reader.recent_annotations(limit, offset)
    names = corrections.titles(ctx, (item.paper_id for item in items))
    return tuple(NoteView(item, names[item.paper_id]) for item in items)
