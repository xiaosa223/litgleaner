"""用户修正论文元数据（ADR 0011）：只在用户看到的地方覆盖，事实层、索引和指纹不受影响。"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ulid import ULID

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_document
from litgleaner.domain import correction
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.paper import Paper


@dataclass(frozen=True)
class MetadataView:
    paper_id: str
    pending: bool  # 来源没有查到元数据（上传 PDF 时没认出标识符）
    corrected: Mapping[str, str]  # 当前生效的用户修正
    title_suggestion: str | None  # 代码从首页认出的标题候选，供预填


def display(ctx: AppContext, paper: Paper) -> Paper:
    return correction.apply(paper, ctx.store.corrections.latest((paper.id,)).get(paper.id, {}))


def display_authors(value: str | None) -> tuple[str, ...]:
    """推送卡片上的作者也按用户修正显示；没有修正时返回空元组。"""
    return correction.split_authors(value) if value else ()


def titles(ctx: AppContext, paper_ids: Iterable[str]) -> dict[str, str]:
    """批量取显示用标题：有用户修正用修正，否则用来源标题。"""
    ids = tuple(dict.fromkeys(paper_ids))
    latest = ctx.store.corrections.latest(ids)
    result: dict[str, str] = {}
    for pid in ids:
        title = latest.get(pid, {}).get("title")
        paper = None if title else ctx.store.papers.get(pid)
        result[pid] = title or (paper.title if paper else pid)
    return result


def pending(ctx: AppContext, paper: Paper) -> bool:
    """来源元数据待补：没有任何标识符，也没有作者。"""
    return not ctx.store.papers.aliases_of(paper.id) and not paper.authors


def _suggestion(ctx: AppContext, paper_id: str) -> str | None:
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is None or state.doc_key is None:
        return None
    try:
        return correction.title_candidate(load_document(ctx, state.doc_key))
    except (KeyError, ValueError):
        return None


def view(ctx: AppContext, paper_id: str) -> MetadataView:
    paper = ctx.store.papers.get(paper_id)
    if paper is None:
        raise NotFound("论文不存在")
    latest = ctx.store.corrections.latest((paper_id,)).get(paper_id, {})
    current = {k: v for k, v in latest.items() if v}
    is_pending = pending(ctx, paper)
    suggestion = _suggestion(ctx, paper_id) if is_pending and "title" not in current else None
    return MetadataView(paper_id, is_pending, current, suggestion)


def save(ctx: AppContext, paper_id: str, fields: Mapping[str, str]) -> MetadataView:
    """只追加与当前生效值不同的字段；空值表示撤销该字段的修正。"""
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound("论文不存在")
    try:
        cleaned = {field: correction.validate(field, value) for field, value in fields.items()}
    except ValueError as error:
        raise InvalidRequest(str(error)) from None
    with ctx.store.atomic():
        latest = ctx.store.corrections.latest((paper_id,)).get(paper_id, {})
        for field, value in cleaned.items():
            if latest.get(field, "") != value:
                ctx.store.corrections.add(
                    correction.Correction(str(ULID()), paper_id, field, value, ctx.now_iso())  # type: ignore[arg-type]
                )
    return view(ctx, paper_id)
