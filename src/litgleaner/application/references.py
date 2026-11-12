"""显式查询参考文献，只保存引文事实，不把候选论文送入收费流程。"""

import hashlib
import logging
from dataclasses import dataclass

from litgleaner.application.citations import store_reference
from litgleaner.application.codec import compress
from litgleaner.application.context import AppContext
from litgleaner.application.source_fetch import fetch, scopes
from litgleaner.domain.errors import InvalidRequest, LitGleanerError, NotFound, SchemaChanged
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.identity import normalize_doi
from litgleaner.domain.query import ReferenceList
from litgleaner.ports.source import Source
from litgleaner.ports.source_capture import CaptureScope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReferenceSourceReport:
    source: str
    fetched: int
    added: int
    candidates: int
    truncated: bool
    warnings: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class ReferenceRefresh:
    paper_id: str
    sources: tuple[ReferenceSourceReport, ...]


def _fetch(
    ctx: AppContext, paper_id: str, source: Source, dois: tuple[str, ...], scope: CaptureScope
) -> ReferenceSourceReport:
    result = fetch(ctx, source, ReferenceList(dois=dois), None, scope)
    if any(normalize_doi(item.citing_doi) not in dois for item in result.references):
        raise SchemaChanged("参考文献响应的主论文标识符不匹配")
    if any(not item.raw_text.strip() for item in result.references):
        raise SchemaChanged("参考文献响应缺少原始条目")
    if result.raw is not None:
        digest = hashlib.sha256(result.raw).hexdigest()
        key = f"raw/references/{stable_hash(source.name)[:16]}/{digest}.zst"
        ctx.blobs.put(key, compress(result.raw))
    now = ctx.now_iso()
    with ctx.store.atomic():
        added = sum(
            store_reference(ctx, paper_id, source.name, item, now) for item in result.references
        )
    return ReferenceSourceReport(
        source.name,
        len(result.references),
        added,
        len(result.records),
        result.truncated,
        result.warnings,
    )


def refresh(
    ctx: AppContext, paper_id: str, source_names: tuple[str, ...] | None = None
) -> ReferenceRefresh:
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound("论文不存在")
    dois = tuple(
        sorted(
            {
                value
                for alias in ctx.store.papers.aliases_of(paper_id)
                if alias.scheme == "doi" and (value := normalize_doi(alias.value))
            }
        )
    )
    if not dois:
        raise InvalidRequest("查询参考文献需要本篇论文的 DOI")
    available = {name: s for name, s in ctx.sources.items() if ReferenceList in s.capabilities}
    names = tuple(dict.fromkeys(source_names)) if source_names is not None else tuple(available)
    if not names or any(name not in available for name in names):
        raise InvalidRequest("没有启用指定的参考文献来源")
    planned = scopes(
        "references",
        {"paper_id": paper_id, "dois": list(dois)},
        [(name, ReferenceList(dois=dois), None) for name in names],
    )
    reports = []
    for name, scope in zip(names, planned, strict=True):
        try:
            reports.append(_fetch(ctx, paper_id, available[name], dois, scope))
        except LitGleanerError as error:
            logger.warning("参考文献来源 %s 查询失败：%s", name, type(error).__name__)
            reports.append(ReferenceSourceReport(name, 0, 0, 0, False, (), type(error).__name__))
    return ReferenceRefresh(paper_id, tuple(reports))
