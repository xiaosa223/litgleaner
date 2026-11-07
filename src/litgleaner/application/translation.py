"""按需段落翻译（第 12.3 节，ADR 0004）。

- 点一段翻一段：流式返回增量，结束后逐句校验（句数、术语表译名），通过才写缓存。
- 译文指纹：原文、目标语言、术语表、译者版本（提示词 + 模型）。
  术语表或模型变了，旧译文仍显示，标为旧版本，不自动重译。
- 中文段落、标题等不翻译。
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from litgleaner.application.context import AppContext
from litgleaner.application.documents import load_document
from litgleaner.application.topics import active_topics
from litgleaner.domain.digest import strip_urls
from litgleaner.domain.document import Block, body_start
from litgleaner.domain.errors import ConfigError, InvalidRequest, NotFound
from litgleaner.domain.glossary import Glossary, check_translation
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.sentences import split_sentences
from litgleaner.ports.translations import CachedTranslation, TranslationKey

TASK = "translate"
TARGET_LANG = "zh"
UNIT = "block"
_MAX_LINE = 2000
_NUMBERING = re.compile(r"^\s*(?:\[\d+\]|\d+[.、)）])\s*")
_CJK = re.compile(r"[㐀-鿿]")
_CJK_RATIO = 0.3


@dataclass(frozen=True)
class TranslationDelta:
    text: str


@dataclass(frozen=True)
class BlockTranslation:
    index: int
    sources: tuple[str, ...]  # 原文分句，与 sentences 一一对应
    sentences: tuple[str, ...]
    verified: bool
    cached: bool
    stale: bool = False  # 用旧术语表或旧模型翻的
    problems: tuple[str, ...] = ()


TranslateEvent = TranslationDelta | BlockTranslation


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
    return stable_hash({"prompt": route.prompt.version, "model": route.model})[:16]


def _blocks(ctx: AppContext, paper_id: str) -> tuple[Block, ...]:
    if ctx.store.papers.get(paper_id) is None:
        raise NotFound(f"论文不存在：{paper_id}")
    state = ctx.store.papers.get_fulltext(paper_id)
    if state is None or state.doc_key is None:
        raise InvalidRequest("这篇论文还没有解析出的正文，无法按段翻译")
    return load_document(ctx, state.doc_key).blocks


def _key(ctx: AppContext, block: Block, glossary: Glossary) -> TranslationKey:
    return TranslationKey(
        UNIT, stable_hash(block.text), TARGET_LANG, glossary.glossary_hash, translator_version(ctx)
    )


def cached_translations(ctx: AppContext, paper_id: str) -> dict[int, BlockTranslation]:
    """已有的段落译文。当前指纹的优先；没有时用最新的旧译文并标记 stale。不调用模型。"""
    all_blocks = _blocks(ctx, paper_id)
    blocks = {i: all_blocks[i] for i in translatable_indices(all_blocks)}
    if not blocks:
        return {}
    glossary = paper_glossary(ctx, paper_id)
    found = ctx.store.translations.latest_for(
        UNIT, (stable_hash(b.text) for b in blocks.values()), TARGET_LANG
    )
    result: dict[int, BlockTranslation] = {}
    for index, block in blocks.items():
        key = _key(ctx, block, glossary)
        mine = [c for c in found if c.key.source_hash == key.source_hash]
        exact = next((c for c in mine if c.key == key), None)
        chosen = exact or (mine[0] if mine else None)
        if chosen is not None:
            result[index] = _from_cache(index, block, chosen, stale=exact is None)
    return result


def _from_cache(
    index: int, block: Block, cached: CachedTranslation, stale: bool
) -> BlockTranslation:
    sources = tuple(split_sentences(block.text))
    return BlockTranslation(
        index, sources, cached.sentences, verified=True, cached=True, stale=stale
    )


def _lines(text: str) -> tuple[str, ...]:
    cleaned = (strip_urls(_NUMBERING.sub("", line).strip()) or "" for line in text.splitlines())
    return tuple(line[:_MAX_LINE] for line in cleaned if line)


def translate_block(ctx: AppContext, paper_id: str, index: int) -> Iterator[TranslateEvent]:
    blocks = _blocks(ctx, paper_id)
    if index not in translatable_indices(blocks):
        raise InvalidRequest("这一块不需要翻译（标题、公式、表格或中文段落）")
    block = blocks[index]
    glossary = paper_glossary(ctx, paper_id)
    key = _key(ctx, block, glossary)
    cached = ctx.store.translations.get(key)
    if cached is not None:
        yield _from_cache(index, block, cached, stale=False)
        return
    sources = tuple(split_sentences(block.text))
    numbered = "\n".join(f"[{n}] {s}" for n, s in enumerate(sources, start=1))
    variables = {"count": str(len(sources)), "glossary": glossary.prompt_text(block.text)}
    stream = ctx.llm.stream(TASK, variables, numbered, paper_id=paper_id)
    for piece in stream:
        yield TranslationDelta(piece)
    if stream.result is None:  # 流异常结束时上面已经抛出，这里只是防御
        raise InvalidRequest("翻译没有完成，请重试")
    lines = _lines(stream.result.text)
    problems = check_translation(sources, lines, glossary)
    if not problems:
        ctx.store.translations.put(key, lines, stream.result.model, paper_id, ctx.now_iso())
    yield BlockTranslation(
        index, sources, lines, verified=not problems, cached=False, problems=problems
    )
