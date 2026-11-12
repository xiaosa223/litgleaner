"""只复核问答实际冻结的来源、关系和模型背景，不扩张旧问题。"""

import json
from collections.abc import Mapping
from copy import copy
from dataclasses import asdict, replace
from typing import Any

from pydantic import ValidationError

from litgleaner.application.qa_memory_checks import (
    actual_sources,
    contains,
    decode_manifest,
    source_problem,
)
from litgleaner.application.stages.extract import extraction_input_hash, extraction_producer_version
from litgleaner.application.stale_inputs import StaleInputs
from litgleaner.application.stale_relations import check_relations
from litgleaner.domain.evidence import clean_untrusted
from litgleaner.domain.hashing import stable_hash
from litgleaner.domain.paper_relation import PaperRelation
from litgleaner.domain.qa import Question
from litgleaner.domain.qa_memory import QASource


def _relations(
    basis: StaleInputs, question: Question, raw: Any, reasons: list[str], unchecked: list[str]
) -> dict[str, PaperRelation]:
    if not isinstance(raw, list):
        unchecked.append("问答缺少完整冻结关系清单")
        return {}
    frozen: dict[str, PaperRelation] = {}
    for item in raw:
        try:
            record = PaperRelation.model_validate(item)
        except (ValidationError, TypeError, ValueError):
            unchecked.append("问答冻结关系结构无效")
            continue
        if record.id in frozen or question.paper_id not in {record.paper_a, record.paper_b}:
            reasons.append("问答冻结关系重复或不属于当前论文")
        frozen[record.id] = record
    current = {record.id: record for record in basis.snapshot.relations}
    selected: list[PaperRelation] = []
    for id_, record in frozen.items():
        found = current.get(id_)
        if found is None or found.status != "valid":
            reasons.append("问答已使用关系已不存在、被拒绝或被替代")
        elif found.model_dump(mode="json") != record.model_dump(mode="json"):
            reasons.append("问答已使用关系的内容已变化")
        else:
            selected.append(found)
    _relation_states(basis, selected, reasons, unchecked)
    return frozen


def _relation_states(
    basis: StaleInputs, selected: list[PaperRelation], reasons: list[str], unchecked: list[str]
) -> None:
    if not selected:
        return
    local = copy(basis)
    batch_ids = {relation.batch_id for relation in selected}
    local.snapshot = replace(
        basis.snapshot,
        relations=tuple(selected),
        relate_batches=tuple(b for b in basis.snapshot.relate_batches if b.id in batch_ids),
    )
    for state in check_relations(local):
        if state.kind == "relation":
            reasons.extend(state.reasons)
            unchecked.extend(state.unchecked)


def _actual_relations(
    relations: Mapping[str, PaperRelation], inputs: Mapping[str, Any], reasons: list[str]
) -> None:
    fields = ("id", "paper_a", "paper_b", "kind", "explanation")
    expected = [
        {key: relation.model_dump(mode="json")[key] for key in fields}
        for relation in relations.values()
    ]
    if inputs.get("relations") != json.loads(clean_untrusted(json.dumps(expected))):
        reasons.append("问答冻结关系与完整清洗消息不一致")


def _backgrounds(
    basis: StaleInputs, question: Question, raw: Any, reasons: list[str], unchecked: list[str]
) -> set[str]:
    if not isinstance(raw, Mapping):
        unchecked.append("问答缺少完整模型背景清单")
        return set()
    producer = extraction_producer_version(basis.ctx) if raw else None
    ids: set[str] = set()
    for paper_id, frozen in raw.items():
        if not isinstance(paper_id, str) or not isinstance(frozen, Mapping):
            unchecked.append("问答模型背景结构无效")
            continue
        ids.add(paper_id)
        if not _in_scope(basis, question, paper_id):
            reasons.append("问答模型背景论文不存在或已退出文库")
            continue
        record = basis.extractions.get(paper_id)
        if record is None:
            reasons.append("问答已使用模型背景的提取已缺失")
            continue
        expected = {
            "id": record.id,
            "input_hash": record.input_hash,
            "producer_version": record.extractor_version,
            "content_hash": stable_hash(asdict(record)),
        }
        if any(key not in frozen for key in expected):
            unchecked.append("问答模型背景缺少完整提取指纹")
        elif any(frozen[key] != value for key, value in expected.items()):
            reasons.append("问答已使用模型背景的最新提取已变化")
        _background_fields(frozen.get("fields"), record.data, reasons, unchecked)
        _background_input(basis, paper_id, producer, reasons, unchecked)
    return ids


def _background_fields(
    fields: Any, data: Mapping[str, Any], reasons: list[str], unchecked: list[str]
) -> None:
    if (
        not isinstance(fields, Mapping)
        or not fields
        or not fields.keys() <= {"summary", "problem", "approach"}
    ):
        unchecked.append("问答模型背景缺少可核对的摘要字段")
        return
    for name, text in fields.items():
        raw = data.get(name)
        canonical = raw.get("text") if isinstance(raw, Mapping) else raw
        if (
            not isinstance(text, str)
            or not isinstance(canonical, str)
            or not contains(text, canonical)
        ):
            reasons.append("问答冻结模型背景与规范提取正文不符")


def _actual_backgrounds(
    manifest: Mapping[str, Any], inputs: Mapping[str, Any], reasons: list[str]
) -> None:
    frozen = manifest.get("backgrounds")
    if not isinstance(frozen, Mapping) or any(
        not isinstance(item, Mapping) for item in frozen.values()
    ):
        return
    expected = {paper_id: item.get("fields") for paper_id, item in frozen.items()}
    if inputs.get("model_backgrounds") != json.loads(clean_untrusted(json.dumps(expected))):
        reasons.append("问答冻结模型背景与完整清洗消息不一致")


def _background_input(
    basis: StaleInputs,
    paper_id: str,
    producer: str | None,
    reasons: list[str],
    unchecked: list[str],
) -> None:
    state, record = basis.snapshot.fulltexts.get(paper_id), basis.extractions[paper_id]
    if state is None or state.doc_key is None:
        reasons.append("问答模型背景缺少当前文档指针")
    else:
        settings = basis.ctx.settings
        expected = extraction_input_hash(
            state.doc_key, settings.chunker, settings.reading_language, settings.assess_token_budget
        )
        if record.input_hash != expected:
            reasons.append("问答模型背景的提取输入已过期")
    if producer is None:
        unchecked.append("未配置提取模型，无法完整核验问答模型背景生产者")
    elif producer != record.extractor_version:
        reasons.append("问答模型背景的提取生产者已变化")


def _in_scope(basis: StaleInputs, question: Question, paper_id: str) -> bool:
    return paper_id in basis.papers and (
        paper_id == question.paper_id or paper_id in basis.snapshot.library_paper_ids
    )


def _titles(
    basis: StaleInputs,
    question: Question,
    manifest: Mapping[str, Any],
    inputs: Mapping[str, Any],
    *,
    ids: set[str],
    reasons: list[str],
    unchecked: list[str],
) -> None:
    frozen, actual = manifest.get("papers"), inputs.get("papers")
    if not isinstance(frozen, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in frozen.items()
    ):
        unchecked.append("问答缺少完整论文身份与标题")
        return
    if set(frozen) != ids or actual != {
        key: clean_untrusted(value) for key, value in frozen.items()
    }:
        reasons.append("问答冻结论文身份或标题与实际发送消息不一致")
    for paper_id, title in frozen.items():
        if not _in_scope(basis, question, paper_id):
            reasons.append("问答已发送论文不存在或已退出文库")
        elif basis.papers[paper_id].title != title:
            reasons.append("问答已发送论文的标题已变化")


def check_sources(
    basis: StaleInputs, question: Question, manifest: Mapping[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reasons: list[str] = []
    unchecked: list[str] = []
    try:
        sources, inputs = decode_manifest(manifest)
    except (ValueError, TypeError) as error:
        return (), (str(error),)
    if manifest["question_hash"] != stable_hash(asdict(question)):
        reasons.append("问答冻结的问题身份或内容已变化")
    if inputs.get("sources") != actual_sources(sources):
        reasons.append("问答冻结来源与完整清洗消息不一致")
    relations = _relations(basis, question, manifest.get("relations"), reasons, unchecked)
    _actual_relations(relations, inputs, reasons)
    ids = _backgrounds(basis, question, manifest.get("backgrounds"), reasons, unchecked)
    _actual_backgrounds(manifest, inputs, reasons)
    ids.update((question.paper_id, *(source.paper_id for source in sources)))
    ids.update(
        paper for relation in relations.values() for paper in (relation.paper_a, relation.paper_b)
    )
    _titles(basis, question, manifest, inputs, ids=ids, reasons=reasons, unchecked=unchecked)
    _sources(basis, question, sources, relations, reasons=reasons, unchecked=unchecked)
    return tuple(dict.fromkeys(reasons)), tuple(dict.fromkeys(unchecked))


def _sources(
    basis: StaleInputs,
    question: Question,
    sources: tuple[QASource, ...],
    relations: Mapping[str, PaperRelation],
    *,
    reasons: list[str],
    unchecked: list[str],
) -> None:
    for source in sources:
        reason, unknown = source_problem(basis, question, source, relations)
        if reason:
            reasons.append(reason)
        if unknown:
            unchecked.append(unknown)
