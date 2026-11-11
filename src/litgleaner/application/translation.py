"""按需翻译（第 12.3 节，ADR 0004）。

- 翻译的单位是段落：被分页、分栏、图表切开的段落先拼回一段；点某一块时翻译它所在的整段。
- 按页或整篇翻译时，按阅读顺序分批，每批约一页正文、每句一个 [[n]] 编号，一段完成就显示。
- 公式、引用、链接等先换成占位符；逐段校验句数、占位符和术语表译名，通过才写缓存。
- 译文指纹：段落原文、目标语言、术语表、译者版本（提示词、模型、规则版本）。
  术语表或模型变了，旧译文仍显示，标为旧版本，不自动重译。
- 中文段落、标题等不翻译。
"""

import logging
import re
from collections.abc import Iterator, Sequence

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_document
from litgleaner.application.topics import active_topics
from litgleaner.application.translation_run import (
    TASK,
    BlockTranslation,
    TranslateEvent,
    TranslationDelta,
    TranslationStatus,
    block_results,
    run_batch,
)
from litgleaner.domain.document import Block, body_start
from litgleaner.domain.errors import ConfigError, InvalidRequest, NotFound, TransientError
from litgleaner.domain.glossary import Glossary
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.numbered import group_batches
from litgleaner.domain.paragraphs import Paragraph, paragraphs
from litgleaner.ports.translations import TranslationKey

__all__ = [
    "BlockTranslation",
    "TranslateEvent",
    "TranslationDelta",
    "TranslationStatus",
    "cached_translations",
    "translate_block",
    "translate_blocks",
]

logger = logging.getLogger(__name__)
TARGET_LANG = "zh"
UNIT = "block"  # 单块段落的指纹与旧的按块缓存相同，旧译文仍能作为旧版本显示
RULES_VERSION = "paragraph-numbered-masked-1"  # 段落合并、编号、占位符规则变了就改
_CJK = re.compile(r"[㐀-鿿]")
_CJK_RATIO = 0.3


def translatable_indices(blocks: tuple[Block, ...]) -> frozenset[int]:
    """正文开始之后的、需要翻译的段落。作者、单位等前置信息不翻译。"""
    start = body_start(blocks)
    return frozenset(i for i, b in enumerate(blocks) if i >= start and translatable(b))


def translatable(block: Block) -> bool:
    text = block.text.strip()
    if block.kind != "text" or not text:
        return False
    return len(_CJK.findall(text)) / len(text) < _CJK_RATIO


def paper_glossary(ctx: AppContext, paper_id: str) -> Glossary:
    """这篇论文命中的主题的术语表合并；没有命中的就用全部启用中的主题。"""
    topics = active_topics(ctx)
    hit = [
        t
        for t, h in ctx.store.judgments.relevant_topics(paper_id)
        if t in topics and topics[t].topic_hash == h
    ]
    merged = Glossary()
    for topic_id in hit or sorted(topics):
        merged = merged.merged(ctx.glossaries.get(topic_id, Glossary()))
    return merged


def translator_version(ctx: AppContext) -> str:
    route = ctx.llm.routes.get(TASK)
    if route is None:
        raise ConfigError("llm.yaml 没有配置 translate 任务，无法翻译")
    return stable_hash(
        {"prompt": route.prompt.version, "model": route.model, "rules": RULES_VERSION}
    )[:16]


def paper_paragraphs(ctx: AppContext, paper_id: str) -> tuple[Paragraph, ...]:
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound(f"论文不存在：{paper_id}")
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is None or state.doc_key is None:
        raise InvalidRequest("这篇论文还没有解析出的正文，无法翻译")
    blocks = load_document(ctx, state.doc_key).blocks
    return paragraphs(blocks, translatable_indices(blocks))


def cache_key(ctx: AppContext, paragraph: Paragraph, glossary: Glossary) -> TranslationKey:
    return TranslationKey(
        UNIT,
        stable_hash(paragraph.text),
        TARGET_LANG,
        glossary.glossary_hash,
        translator_version(ctx),
    )


def cached_translations(ctx: AppContext, paper_id: str) -> dict[int, BlockTranslation]:
    """已有的译文，按块给出。当前指纹的优先；没有时用最新的旧译文并标记 stale。不调用模型。"""
    found_paragraphs = paper_paragraphs(ctx, paper_id)
    if not found_paragraphs:
        return {}
    glossary = paper_glossary(ctx, paper_id)
    found = ctx.store.translations.latest_for(
        UNIT, (stable_hash(p.text) for p in found_paragraphs), TARGET_LANG
    )
    result: dict[int, BlockTranslation] = {}
    for paragraph in found_paragraphs:
        key = cache_key(ctx, paragraph, glossary)
        mine = [
            c
            for c in found
            if c.key.source_hash == key.source_hash and len(c.sentences) == len(paragraph.sentences)
        ]
        exact = next((c for c in mine if c.key == key), None)
        chosen = exact or (mine[0] if mine else None)
        if chosen is not None:
            blocks = block_results(paragraph, chosen.sentences, cached=True, stale=exact is None)
            result.update((b.index, b) for b in blocks)
    return result


def translate_block(ctx: AppContext, paper_id: str, index: int) -> Iterator[TranslateEvent]:
    """翻译某一块所在的整段，给出这一段每一块的结果。"""
    found = paper_paragraphs(ctx, paper_id)
    target = next((p for p in found if index in p.indices), None)
    if target is None:
        raise InvalidRequest("这一块不需要翻译（标题、公式、表格或中文段落）")
    yield from _translate(ctx, paper_id, [target], retry=False)


def translate_blocks(
    ctx: AppContext, paper_id: str, indices: Sequence[int]
) -> Iterator[TranslateEvent]:
    """按页或整篇翻译：包含这些块的段落按阅读顺序分批翻译，已缓存的直接给出。"""
    wanted = set(indices)
    found = paper_paragraphs(ctx, paper_id)
    chosen = [p for p in found if wanted.intersection(p.indices)]
    if not chosen:
        raise InvalidRequest("选中的部分没有需要翻译的段落")
    yield from _translate(ctx, paper_id, chosen, retry=True)


def _translate(
    ctx: AppContext, paper_id: str, chosen: Sequence[Paragraph], *, retry: bool
) -> Iterator[TranslateEvent]:
    glossary = paper_glossary(ctx, paper_id)
    model = ctx.llm.routes[TASK].model if TASK in ctx.llm.routes else ""

    def save(paragraph: Paragraph, sentences: tuple[str, ...]) -> None:
        key = cache_key(ctx, paragraph, glossary)
        ctx.store.translations.put(key, sentences, model, paper_id, ctx.now_iso())

    pending: list[Paragraph] = []
    for paragraph in chosen:
        cached = ctx.store.translations.get(cache_key(ctx, paragraph, glossary))
        if cached is not None and len(cached.sentences) == len(paragraph.sentences):
            yield from block_results(paragraph, cached.sentences, cached=True)
        else:
            pending.append(paragraph)
    for batch in group_batches([len(p.text) for p in pending]):
        group = [pending[i] for i in batch]
        if not retry or len(group) == 1:
            yield from run_batch(ctx, paper_id, group, glossary, save)
            continue
        translated: set[int] = set()  # 已经拿到译文的块
        try:
            for event in run_batch(ctx, paper_id, group, glossary, save):
                if isinstance(event, BlockTranslation) and event.sentences:
                    translated.add(event.index)
                yield event
        except TransientError:  # 整批中途失败（例如推理模型输出被截断）：已完成的段落保留
            logger.warning("整批翻译中途失败，没完成的段落逐段重试", exc_info=True)
        for paragraph in group:  # 结构出错或没轮到的段落单独再翻一次
            if not translated.issuperset(paragraph.indices):
                yield from run_batch(ctx, paper_id, [paragraph], glossary, save)
