"""关系候选逐侧核验，未发送尾部、错误身份或页码不能成为证据。"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError

from litgleaner.application.context import AppContext
from litgleaner.application.extraction_facts import verify_extraction
from litgleaner.application.relate_schema import RELATION_SCHEMA
from litgleaner.application.relate_sources import RelateSource
from litgleaner.domain.evidence import clean_untrusted
from litgleaner.domain.paper_relation import (
    RelationEvidence,
    RelationProposal,
    canonical_relation,
)

if TYPE_CHECKING:
    from litgleaner.application.relate_input import RelateInput


def _sent_pairs(prepared: RelateInput) -> frozenset[tuple[str, str]]:
    """按清洗后完整消息中的 JSON 值核对实际片段，保留换行和转义语义。"""
    try:
        data = json.loads(clean_untrusted(prepared.text))
        pairs = set()
        for pair in data["pairs"]:
            left, right = pair["paper_a"], pair["paper_b"]
            if any(record["paper_id"] not in prepared.sources for record in (left, right)):
                continue
            for record in (left, right):
                if record != prepared.sources[record["paper_id"]].record:
                    return frozenset()
            if left["paper_id"] == right["paper_id"]:
                return frozenset()
            pairs.add(tuple(sorted((left["paper_id"], right["paper_id"]))))
        return frozenset(pairs)
    except (ValueError, TypeError, KeyError):
        return frozenset()


def _pair(raw: Any) -> tuple[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    a, b = raw.get("paper_a"), raw.get("paper_b")
    if not isinstance(a, str) or not isinstance(b, str):
        return None
    return (a, b) if a < b else (b, a)


def _evidence(raw: Any, paper_id: str, source: RelateSource) -> RelationEvidence | None:
    if not isinstance(raw, Mapping):
        return None
    if raw.get("paper_id") != paper_id or raw.get("doc_key") != source.doc_key:
        return None
    checked = verify_extraction({"claims": [raw]}, source.document, source.source)
    if checked["claims"][0]["verified"] is not True:
        return None
    try:
        return RelationEvidence.model_validate(raw)
    except ValidationError:
        return None


def _proposal(prepared: RelateInput, raw: Mapping[str, Any]) -> RelationProposal | None:
    a, b = raw["paper_a"], raw["paper_b"]
    source_a, source_b = prepared.sources.get(a), prepared.sources.get(b)
    if source_a is None or source_b is None:
        return None
    left = _evidence(raw["evidence_a"], a, source_a)
    right = _evidence(raw["evidence_b"], b, source_b)
    if left is None or right is None:
        return None
    try:
        proposal = RelationProposal(
            paper_a=a,
            paper_b=b,
            kind=raw["kind"],
            evidence_a=left,
            evidence_b=right,
            explanation=raw["explanation"],
            source_hashes={a: prepared.source_hashes[a], b: prepared.source_hashes[b]},
        )
        return canonical_relation(proposal)
    except ValidationError:
        return None


def validate_result(
    ctx: AppContext, prepared: RelateInput, raw: Mapping[str, Any]
) -> tuple[tuple[RelationProposal, ...], tuple[str, ...]]:
    """全部无关或失败也返回空集合，调用者仍须保存完成批次。"""
    values = raw.get("relations")
    if not isinstance(values, list) or len(values) > 200:
        return (), ("关系输出不是有效列表，未采纳任何关系",)
    pairs = _sent_pairs(prepared)
    sent = frozenset(prepared.paper_ids)
    counts = Counter(pair for item in values if (pair := _pair(item)) is not None)
    proposals, warnings = [], []
    validator = Draft202012Validator(RELATION_SCHEMA)
    for index, value in enumerate(values):
        pair = _pair(value)
        if (
            pair is not None
            and set(pair) <= sent
            and any(key not in prepared.sources for key in pair)
        ):
            warnings.append(f"关系{index + 1}：当前原文不可读或已发送片段已变化，已丢弃")
        elif pair not in pairs or pair is None:
            warnings.append(f"关系{index + 1}：双端不属于实际发送的论文对，已丢弃")
        elif counts[pair] != 1:
            warnings.append(f"关系{index + 1}：同一论文对重复或冲突，已丢弃")
        elif not validator.is_valid(value):
            warnings.append(f"关系{index + 1}：字段结构无效，已丢弃")
        elif value["kind"] is not None:
            proposal = _proposal(prepared, value)
            if proposal is None:
                warnings.append(f"关系{index + 1}：双侧原文核验未通过，已丢弃")
            else:
                proposals.append(proposal)
    return tuple(proposals), tuple(dict.fromkeys(warnings))
