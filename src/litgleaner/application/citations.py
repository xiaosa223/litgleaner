"""引文事实的摄取与查询；只按精确标识符关联论文。"""

from dataclasses import asdict, dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_chunks, load_document
from litgleaner.domain.citation import Citation, ReferenceRecord, SourceCitation, reference_aliases
from litgleaner.domain.document import NormalizedDocument
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.identity import Alias, record_aliases
from litgleaner.domain.paper import SourceRecord
from litgleaner.domain.sections import split_sections


@dataclass(frozen=True)
class CitationView:
    citation: Citation
    citing_title: str
    cited_paper_id: str | None
    cited_title: str | None


@dataclass(frozen=True)
class CitationList:
    outgoing: tuple[CitationView, ...]
    incoming: tuple[CitationView, ...]


def exact_match(ctx: AppContext, aliases: tuple[Alias, ...]) -> str | None:
    ids = ctx.store.papers.find_ids_by_aliases(aliases)
    return next(iter(ids)) if len(ids) == 1 else None


def store_reference(
    ctx: AppContext, paper_id: str, source: str, item: ReferenceRecord, now: str
) -> bool:
    position = f"{source}:{item.position}:{stable_hash(asdict(item))}"
    return ctx.store.citations.add(
        Citation(
            paper_id,
            position,
            item.raw_text,
            source,
            item.position,
            exact_match(ctx, item.cited_aliases),
            item.cited_title,
            item.cited_aliases,
            item.contexts,
            item.intents,
            item.influential,
            now,
        )
    )


def _source_fingerprint(source: str, item: SourceCitation) -> str:
    evidence = asdict(item)
    evidence.pop("source_position")
    for field in ("citing_aliases", "cited_aliases"):
        evidence[field] = sorted({(alias.scheme, alias.value) for alias in getattr(item, field)})
    return stable_hash({"source": source, "evidence": evidence})


def ingest_source_citations(
    ctx: AppContext,
    source: str,
    items: tuple[SourceCitation, ...],
    records: tuple[SourceRecord, ...],
    now: str,
) -> None:
    """在发现事务中只登记本批已采纳且能唯一精确关联的引用者关系。"""
    accepted = {match for record in records if (match := exact_match(ctx, record_aliases(record)))}
    for item in items:
        citing = exact_match(ctx, item.citing_aliases)
        if citing is None or citing not in accepted or not item.cited_aliases:
            continue
        ctx.store.citations.add(
            Citation(
                citing,
                f"source:{source}:{_source_fingerprint(source, item)}",
                item.raw_text,
                source,
                item.source_position,
                exact_match(ctx, item.cited_aliases),
                None,
                item.cited_aliases,
                item.contexts,
                item.intents,
                item.influential,
                now,
            )
        )


def ingest_parsed(
    ctx: AppContext,
    paper_id: str,
    document: NormalizedDocument,
    references: tuple[str, ...],
    now: str,
) -> None:
    """和全文状态在同一事务中调用；旧文档不在查询时自动解析或补写。"""
    blocks = [
        b
        for section in split_sections(document)
        for b in section.blocks
        if b.kind == "reference" or section.section_type == "references"
    ]
    for index, text in enumerate(references):
        if not text.strip():
            continue
        block = next((b for b in blocks if b.text == text), None)
        position = stable_hash(
            {"pdf": document.source_pdf_sha256, "parser": document.parser_id, "text": text}
        )
        aliases = reference_aliases(text)
        ctx.store.citations.add(
            Citation(
                paper_id,
                f"parsed:{index}:{position}",
                text,
                document.parser_id,
                index,
                exact_match(ctx, aliases),
                None,
                aliases,
                (),
                (),
                None,
                now,
                block.page if block else None,
                block.bbox if block else None,
            )
        )


def _view(ctx: AppContext, item: Citation) -> CitationView:
    citing = ctx.store.papers.get(item.citing_paper_id)
    cited_id = item.cited_paper_id or exact_match(ctx, item.cited_aliases)
    cited = ctx.store.papers.get(cited_id) if cited_id else None
    return CitationView(
        item,
        citing.title if citing else item.citing_paper_id,
        cited_id,
        cited.title if cited else item.cited_title,
    )


def for_paper(ctx: AppContext, paper_id: str) -> CitationList:
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound("论文不存在")
    return CitationList(
        tuple(_view(ctx, item) for item in ctx.store.citations.outgoing(paper_id)),
        tuple(_view(ctx, item) for item in ctx.store.citations.incoming(paper_id)),
    )


def index_parsed(ctx: AppContext, paper_id: str) -> CitationList:
    """显式复用旧归档重建引文，不重新解析 PDF、不请求来源或模型。"""
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound("论文不存在")
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is None or state.doc_key is None:
        raise InvalidRequest("这篇论文还没有解析出的正文")
    document = load_document(ctx, state.doc_key)
    references = load_chunks(ctx, state.doc_key).references
    with ctx.store.atomic():
        ingest_parsed(ctx, paper_id, document, references, ctx.now_iso())
    return for_paper(ctx, paper_id)
