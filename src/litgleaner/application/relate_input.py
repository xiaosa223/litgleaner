"""首次关系核实的只读冻结输入；整对受完整消息预算限制。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from litgleaner.application.context import AppContext
from litgleaner.application.memory_candidates import project_candidates
from litgleaner.application.qa_context import qa_token_count
from litgleaner.application.relate_evidence import validate_result
from litgleaner.application.relate_schema import RELATE_CONTRACT, RELATE_SCHEMA
from litgleaner.application.relate_sources import (
    RelatePreparationCache,
    RelateSource,
    prepare_sources,
    refresh_sources,
    source_hash,
)
from litgleaner.application.stages.extract import extraction_producer_version
from litgleaner.domain.errors import InvalidRequest, NotFound
from litgleaner.domain.evidence import clean_untrusted, wrap_untrusted
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.language import language_name
from litgleaner.ports.memory_candidates import CandidateSnapshot

__all__ = [
    "RelateInput",
    "RelatePreparationCache",
    "configuration",
    "prepare_input",
    "producer_version",
    "refresh_documents",
    "source_hash",
    "validate_result",
]


@dataclass(frozen=True)
class RelateInput:
    text: str
    system: str
    input_hash: str
    producer_version: str | None
    eligible_ids: tuple[str, ...]
    considered_ids: tuple[str, ...]
    paper_ids: tuple[str, ...]
    source_hashes: dict[str, str]
    tokens: int
    warnings: tuple[str, ...]
    pairs_count: int
    estimated_usd: float | None
    sources: Mapping[str, RelateSource]


def producer_version(ctx: AppContext) -> str | None:
    route = ctx.llm.routes.get("relate")
    return (
        None
        if route is None
        else stable_hash(
            {
                "contract": RELATE_CONTRACT,
                "schema": RELATE_SCHEMA,
                "prompt": route.prompt.version,
                "model": route.model,
                "provider": route.provider_version,
            }
        )
    )


def configuration(ctx: AppContext) -> Mapping[str, Any]:
    """费用上限和瞬时就绪状态不改变一次性事件或派生物身份。"""
    return {
        "contract": RELATE_CONTRACT,
        "policy": ctx.settings.memory_policy.model_dump(mode="json"),
        "policy_hash": ctx.settings.memory_policy.policy_hash,
        "relate": ctx.settings.relate.model_dump(mode="json"),
        "reading_language": ctx.settings.reading_language,
        "extract_producer": extraction_producer_version(ctx),
        "extract_budget": ctx.settings.assess_token_budget,
        "chunker": ctx.settings.chunker.version,
        "producer": producer_version(ctx),
        "embedding": ctx.settings.embedding.model_dump(mode="json"),
        "embedding_configuration": None
        if ctx.embedder is None
        else ctx.embedder.describe().configuration_version,
    }


def _json(pairs: list[dict[str, Any]]) -> str:
    return clean_untrusted(json.dumps({"pairs": pairs}, ensure_ascii=False, sort_keys=True))


def _estimate(ctx: AppContext, tokens: int, count: int) -> float | None:
    if not count:
        return 0.0
    route = ctx.llm.routes.get("relate")
    if route is None:
        return None
    price = route.llm.price(route.model)
    if price is None:
        return None
    return (tokens * price[0] + count * 1200 * price[1]) / 1_000_000


def _scoped(
    ctx: AppContext,
    paper_id: str,
    eligible_ids: tuple[str, ...] | None,
    cache: RelatePreparationCache | None,
) -> tuple[CandidateSnapshot, tuple[str, ...], dict[str, RelateSource], tuple[str, ...]]:
    data = ctx.store.memory_candidates_snapshot(
        paper_id, ctx.settings.memory_policy.query_terms_limit
    )
    library = {paper.id for paper in data.facts.papers}
    if paper_id not in library:
        raise NotFound(f"论文尚未进入文库或不存在：{paper_id}")
    eligible = (library if eligible_ids is None else library & set(eligible_ids)) - {paper_id}
    frozen_eligible = tuple(sorted(eligible))
    decided = ctx.store.relations.user_decided_pairs()
    eligible = {key for key in eligible if tuple(sorted((paper_id, key))) not in decided}
    sources, warnings = prepare_sources(
        ctx, data.facts, frozenset({paper_id, *eligible}), cache=cache
    )
    facts = replace(data.facts, papers=tuple(p for p in data.facts.papers if p.id in sources))
    # 全局 aliases 保留；过滤候选不能把歧义目标假装成唯一身份。
    return replace(data, facts=facts), frozen_eligible, sources, warnings


def _select(
    ctx: AppContext,
    paper_id: str,
    data: CandidateSnapshot,
    sources: Mapping[str, RelateSource],
    system: str,
) -> tuple[list[dict[str, Any]], tuple[str, ...], tuple[str, ...]]:
    if paper_id not in sources:
        return [], (paper_id,), ()
    ranked = project_candidates(ctx, paper_id, data)
    considered = (paper_id, *(item.facts.paper.paper_id for item in ranked.matches))
    pairs: list[dict[str, Any]] = []
    omitted: list[str] = []
    for key in considered[1:]:
        pair = {"paper_a": sources[paper_id].record, "paper_b": sources[key].record}
        if (
            qa_token_count(system + wrap_untrusted(_json([*pairs, pair])))
            > ctx.settings.relate.token_budget
        ):
            omitted.append(key)
        else:
            pairs.append(pair)
    warnings = tuple(f"{key}：整对超过关系输入预算，未发送" for key in omitted)
    return pairs, considered, (*warnings, *(f"{w.paper_id}：{w.detail}" for w in ranked.warnings))


def prepare_input(
    ctx: AppContext,
    paper_id: str,
    eligible_ids: tuple[str, ...] | None = None,
    *,
    cache: RelatePreparationCache | None = None,
) -> RelateInput:
    route = ctx.llm.routes.get("relate")
    system = (
        ""
        if route is None
        else route.prompt.render({"output_language": language_name(ctx.settings.reading_language)})
    )
    if qa_token_count(system + wrap_untrusted(_json([]))) > ctx.settings.relate.token_budget:
        raise InvalidRequest("关系提示词超过输入预算，请缩短提示词或提高 relate.token_budget")
    data, eligible, sources, warnings = _scoped(ctx, paper_id, eligible_ids, cache)
    pairs, considered, selection_warnings = _select(ctx, paper_id, data, sources, system)
    text = _json(pairs)
    paper_ids = tuple(sorted({side["paper_id"] for pair in pairs for side in pair.values()}))
    sent = {key: sources[key] for key in paper_ids}
    hashes = {key: source.source_hash for key, source in sent.items()}
    tokens = qa_token_count(system + wrap_untrusted(text))
    estimated = _estimate(ctx, tokens, len(pairs))
    if pairs:
        warnings = (*warnings, "单侧仅发送限量连续原文；关系为模型判断，原文存在性由代码核验")
        warnings = (
            *warnings,
            "模型未提供价格，暂不能估算费用"
            if estimated is None
            else "费用为按实际输入和每对预估输出计算的经验估算",
        )
    identity = stable_hash(
        {"configuration": configuration(ctx), "text": text, "system": system, "sources": hashes}
    )
    return RelateInput(
        text,
        system,
        identity,
        producer_version(ctx),
        eligible,
        considered,
        paper_ids,
        hashes,
        tokens,
        tuple(dict.fromkeys((*warnings, *selection_warnings))),
        len(pairs),
        estimated,
        sent,
    )


def refresh_documents(ctx: AppContext, prepared: RelateInput) -> RelateInput:
    """按实际已发来源强制重读 distinct 文档，供付费返回后的逐对核验。"""
    return replace(prepared, sources=refresh_sources(ctx, prepared.sources))
