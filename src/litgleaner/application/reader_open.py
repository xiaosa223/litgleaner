"""打开阅读页的明确写操作；重复打开不降级既有阅读状态。"""

from litgleaner.application import reader
from litgleaner.application.context import AppContext
from litgleaner.domain.errors import NotFound
from litgleaner.domain.reading import ReadingState


def open_paper(ctx: AppContext, paper_id: str) -> ReadingState:
    with ctx.store.atomic():
        if ctx.store.papers.get(paper_id) is None:
            raise NotFound("论文不存在")
        current = ctx.store.reader.get_state(paper_id)
        if current is not None:
            return current
        reader.set_reading_state(ctx, paper_id, "reading", None)
        result = ctx.store.reader.get_state(paper_id)
        assert result is not None
        return result
